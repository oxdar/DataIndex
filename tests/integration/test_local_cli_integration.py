"""The ``dataindex`` local commands end to end.

``submit``, ``submit-db``, ``documents`` and ``chat`` run in-process through
``dataindex.cli.main`` with the real first-party plugins. Chat reaches a loopback
OpenAI-compatible server whose replies each test scripts, so there is no network and
no API key.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from dataindex.cli import main
from dataindex.llm import SYSTEM_PROMPT
from fake_chat_server import (
    HttpFailure,
    answer_reply,
    is_repair,
    planner_payload,
    planner_responder,
    scripted,
    system_prompt,
)
from local_flow_helpers import (
    CLAIM,
    DB_CLAIM,
    DB_QUESTION,
    INJECTION_MARKERS,
    MODEL,
    OVERDUE_QUERY_ID,
    QUESTION,
    database_config,
    database_policy,
    document_policy,
    find_node,
    make_database,
    make_document,
    path_variants,
    query_ids,
    serve,
    serve_raw,
    walk_nodes,
    write_injection_document,
)

STALE = "file changed after it was submitted"
NO_MODEL = "no chat model; pass --model NAME or set DATAINDEX_MODEL"
CONFIG_SHAPE = (
    "must be a JSON object with an 'allowed_columns' object and a 'queries' list, "
    "and no other keys"
)
SAMPLE_CONFIG = Path(__file__).resolve().parents[2] / "examples" / "contracts_database_config.json"
API_KEY = "test-key-0123456789"  # an obvious placeholder, not a credential


@dataclass(frozen=True)
class Run:
    """The exit status and the captured output of one command."""

    code: int
    out: str
    err: str

    def json(self) -> Any:
        return json.loads(self.out)

    def json_objects(self) -> list[Any]:
        """Every JSON document printed one after the other, as ``chat`` does in a loop."""
        decoder = json.JSONDecoder()
        objects: list[Any] = []
        position = 0
        while True:
            while position < len(self.out) and self.out[position].isspace():
                position += 1
            if position == len(self.out):
                return objects
            value, position = decoder.raw_decode(self.out, position)
            objects.append(value)


class Cli:
    """Runs ``dataindex`` commands in-process against one workspace directory."""

    def __init__(
        self,
        storage: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self.storage = storage
        self._capsys = capsys
        self._monkeypatch = monkeypatch

    def __call__(
        self,
        *argv: str,
        stdin: str | None = None,
        storage: Path | None = None,
        expect: int = 0,
    ) -> Run:
        if stdin is not None:
            self._monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        code = main([*argv, "--storage", str(storage or self.storage)])
        captured = self._capsys.readouterr()
        run = Run(code, captured.out, captured.err)
        assert run.code == expect, run
        return run


@pytest.fixture
def cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Cli:
    # A command that forgot --storage would write below tmp_path, never into the repo.
    monkeypatch.chdir(tmp_path)
    return Cli(tmp_path / ".dataindex", capsys, monkeypatch)


def error_line(run: Run) -> str:
    """The message of a handled error: status 2, nothing on stdout, one line on stderr."""
    assert (run.code, run.out) == (2, ""), run
    assert run.err.startswith("dataindex: ") and run.err.count("\n") == 1, run
    assert "Traceback" not in run.err
    return run.err.removeprefix("dataindex: ").rstrip("\n")


def submit_id(cli: Cli, path: Path, *options: str) -> str:
    return str(cli("submit", str(path), "--json", *options).json()["doc_id"])


def write_config(directory: Path, config: Any = None, name: str = "contracts.json") -> Path:
    """A database configuration file; ``config`` is a JSON value or raw text."""
    content = database_config() if config is None else config
    target = directory / name
    text = content if isinstance(content, str) else json.dumps(content)
    target.write_text(text, encoding="utf-8")
    return target


def submit_database_id(cli: Cli, directory: Path, database: Path) -> str:
    config = write_config(directory)
    registered = cli("submit-db", str(database), "--config", str(config), "--json")
    return str(registered.json()["doc_id"])


class Terminal(io.StringIO):
    """Standard input that the chat loop takes for a person at a keyboard."""

    def isatty(self) -> bool:
        return True


class InterruptedTerminal(Terminal):
    """A keyboard where the person presses Ctrl+C at the prompt."""

    def readline(self, size: int = -1) -> str:
        raise KeyboardInterrupt


# --- submit ------------------------------------------------------------------------


def test_submit_confirms_the_document_and_a_second_submit_changes_nothing(
    cli: Cli, tmp_path: Path
) -> None:
    source = make_document(tmp_path / "docs")

    first = cli("submit", str(source))
    created = re.fullmatch(
        r"Submitted terms\.md as (doc-[0-9a-f]{16}) \(new, 4 nodes\)\n", first.out
    )
    assert created is not None and first.err == ""

    again = cli("submit", str(source))
    assert again.out == f"Submitted terms.md as {created[1]} (unchanged, 4 nodes)\n"
    assert cli("documents", "list", "--json").json()["total"] == 1


def test_submit_json_describes_the_document_and_follows_the_file(
    cli: Cli, tmp_path: Path
) -> None:
    source = make_document(tmp_path / "docs")
    version = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()

    first = cli("submit", str(source), "--json")
    payload = first.json()
    assert first.err == ""
    assert re.fullmatch(r"doc-[0-9a-f]{16}", payload["doc_id"])
    assert payload == {
        "doc_id": payload["doc_id"],
        "name": "terms.md",
        "status": "completed",
        "change": "new",
        "source_type": "text/markdown",
        "version": version,
        "node_count": 4,
    }
    assert cli("submit", str(source), "--json").json()["change"] == "unchanged"

    source.write_bytes(source.read_bytes() + b"\r\nA clause added later.\r\n")
    changed = cli("submit", str(source), "--json").json()
    assert (changed["doc_id"], changed["change"]) == (payload["doc_id"], "updated")
    assert changed["version"] != version


def test_submit_name_sets_the_display_name_and_bad_names_are_refused(
    cli: Cli, tmp_path: Path
) -> None:
    source = make_document(tmp_path / "docs")

    named = cli("submit", str(source), "--name", "Payment guide")
    assert named.out.startswith("Submitted Payment guide as doc-")
    listed = cli("documents", "list", "--json").json()["documents"]
    assert [item["name"] for item in listed] == ["Payment guide"]

    assert "name must" in error_line(cli("submit", str(source), "--name", "a\nb", expect=2))
    assert cli("documents", "list", "--json").json()["total"] == 1


def test_a_second_file_with_the_same_name_is_renamed_and_the_cli_warns(
    cli: Cli, tmp_path: Path
) -> None:
    first = make_document(tmp_path / "one")
    second = make_document(tmp_path / "two")
    assert cli("submit", str(first)).err == ""

    renamed = cli("submit", str(second))

    assert re.fullmatch(
        r"Submitted terms_1\.md as doc-[0-9a-f]{16} \(new, 4 nodes\)\n", renamed.out
    )
    assert renamed.err.startswith(
        "dataindex: warning: a document named 'terms.md' already exists; "
        "this one is named 'terms_1.md'"
    )
    assert renamed.err.count("\n") == 1
    names = [item["name"] for item in cli("documents", "list", "--json").json()["documents"]]
    assert names == ["terms_1.md", "terms.md"]
    # Resubmitting the renamed file keeps its name and does not warn again.
    assert cli("submit", str(second)).err == ""


def test_a_sqlite_file_given_to_submit_is_redirected_to_submit_db(
    cli: Cli, tmp_path: Path
) -> None:
    database = make_database(tmp_path / "dbs")

    message = error_line(cli("submit", str(database), expect=2))

    assert message == (
        "'contracts.sqlite' is a SQLite database; submit it with: "
        "dataindex submit-db PATH --config FILE"
    )
    assert cli("documents", "list", "--json").json()["total"] == 0


def test_submit_reports_a_missing_file_as_a_handled_error(cli: Cli, tmp_path: Path) -> None:
    error_line(cli("submit", str(tmp_path / "missing.md"), expect=2))

    assert cli("documents", "list").out.startswith("No documents have been submitted")


# --- submit-db ---------------------------------------------------------------------


def test_submit_db_registers_a_database_with_only_its_approved_queries(
    cli: Cli, tmp_path: Path
) -> None:
    database = make_database(tmp_path / "dbs")
    config = write_config(tmp_path)

    registered = cli("submit-db", str(database), "--config", str(config))
    assert re.fullmatch(
        r"Submitted contracts\.sqlite as db-[0-9a-f]{16} \(new, \d+ nodes\)\n",
        registered.out,
    )
    assert registered.err == ""

    again = cli("submit-db", str(database), "--config", str(config), "--json").json()
    assert again["change"] == "unchanged"
    assert again["source_type"] == "application/vnd.sqlite3"
    assert again["version"].startswith("sqlite-state:")

    info = cli("documents", "get", "contracts.sqlite").json()
    assert (info["kind"], info["status"], info["name"]) == (
        "database",
        "completed",
        "contracts.sqlite",
    )
    assert {item["query_id"] for item in info["queries"]} == query_ids()
    advertised = json.dumps(info["queries"])
    assert "tenant-a" not in advertised  # the fixed source filter stays hidden
    assert not re.search(r"\b(select|from|where|join)\b", advertised, re.I)


BAD_CONFIGS = {
    "invalid JSON": ("{not json", "is not valid JSON"),
    "a JSON array": ("[]", CONFIG_SHAPE),
    "an unknown key": (json.dumps({**database_config(), "tenant_id": "tenant-b"}), CONFIG_SHAPE),
    "no queries": (json.dumps({"allowed_columns": {}}), CONFIG_SHAPE),
    "queries that are not a list": (
        json.dumps({"allowed_columns": {}, "queries": {}}),
        CONFIG_SHAPE,
    ),
    "a file over 256 KiB": (" " * (256 * 1024 + 1), "is larger than 256 KiB"),
}


@pytest.mark.parametrize("name", list(BAD_CONFIGS))
def test_submit_db_refuses_a_malformed_configuration_file(
    cli: Cli, tmp_path: Path, name: str
) -> None:
    text, fragment = BAD_CONFIGS[name]
    database = make_database(tmp_path / "dbs")
    config = write_config(tmp_path, text, "bad.json")

    message = error_line(cli("submit-db", str(database), "--config", str(config), expect=2))

    assert message.startswith("'bad.json' ") and fragment in message
    assert cli("documents", "list", "--json").json()["total"] == 0


def test_submit_db_refuses_a_missing_configuration_and_a_file_that_is_not_a_database(
    cli: Cli, tmp_path: Path
) -> None:
    database = make_database(tmp_path / "dbs")
    source = make_document(tmp_path / "docs")
    config = write_config(tmp_path)

    error_line(cli("submit-db", str(database), "--config", str(tmp_path / "gone.json"), expect=2))
    message = error_line(cli("submit-db", str(source), "--config", str(config), expect=2))

    assert message == (
        "'terms.md' is not a SQLite database; submit documents with: dataindex submit PATH"
    )
    assert cli("documents", "list", "--json").json()["total"] == 0


def test_the_documented_sample_configuration_registers_the_fixture_database(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = make_database(tmp_path / "dbs")

    registered = cli("submit-db", str(database), "--config", str(SAMPLE_CONFIG), "--json").json()

    assert (registered["change"], registered["source_type"]) == ("new", "application/vnd.sqlite3")
    queries = cli("documents", "get", "contracts.sqlite").json()["queries"]
    assert [item["query_id"] for item in queries] == [OVERDUE_QUERY_ID]
    with serve(monkeypatch, database_policy(), model=MODEL):
        answer = cli("chat", DB_QUESTION, "--doc", "contracts.sqlite", "--json").json()
    assert [item["operation"] for item in answer["citations"]] == ["databases.query"]


def test_a_rejected_query_registers_nothing_and_never_replaces_a_working_registration(
    cli: Cli, tmp_path: Path
) -> None:
    database = make_database(tmp_path / "dbs")
    working = write_config(tmp_path)
    narrowed = database_config()
    narrowed["allowed_columns"]["invoices"].remove("amount_cents")
    rejected = write_config(tmp_path, narrowed, "narrowed.json")

    message = error_line(cli("submit-db", str(database), "--config", str(rejected), expect=2))
    assert message.startswith("'contracts.sqlite' was rejected: ")
    assert "non-allowlisted column" in message
    assert cli("documents", "list", "--json").json()["total"] == 0

    cli("submit-db", str(database), "--config", str(working))
    before = cli("documents", "get", "contracts.sqlite").json()
    error_line(cli("submit-db", str(database), "--config", str(rejected), expect=2))
    assert cli("documents", "get", "contracts.sqlite").json() == before


# --- documents ---------------------------------------------------------------------


def test_documents_list_on_an_empty_workspace_says_how_to_add_one(cli: Cli) -> None:
    empty = cli("documents", "list")

    assert empty.out == "No documents have been submitted. Add one with: dataindex submit PATH\n"
    assert empty.err == ""
    assert cli("documents", "list", "--json").json() == {
        "documents": [],
        "total": 0,
        "limit": 50,
        "offset": 0,
    }


def test_documents_list_shows_the_newest_first_and_hints_at_the_next_page(
    cli: Cli, tmp_path: Path
) -> None:
    ids = {
        name: submit_id(cli, make_document(tmp_path / "docs", name))
        for name in ("a.md", "b.md", "c.md")
    }

    table = cli("documents", "list").out.splitlines()
    assert table[0].split() == ["ID", "KIND", "STATUS", "NAME"]
    assert [line.split() for line in table[1:]] == [
        [ids[name], "document", "completed", name] for name in ("c.md", "b.md", "a.md")
    ]

    first = cli("documents", "list", "--limit", "1").out.splitlines()
    assert first[1].split()[-1] == "c.md"
    assert first[-1] == "Showing 1-1 of 3; use --offset 1 for more."
    middle = cli("documents", "list", "--limit", "1", "--offset", "1").out.splitlines()
    assert middle[1].split()[-1] == "b.md"
    assert middle[-1] == "Showing 2-2 of 3; use --offset 2 for more."
    last = cli("documents", "list", "--limit", "1", "--offset", "2").out.splitlines()
    assert [line.split()[-1] for line in last[1:]] == ["a.md"]  # no hint on the last page
    beyond = cli("documents", "list", "--offset", "9")
    assert beyond.out == "No documents at offset 9; the workspace holds 3.\n"

    page = cli("documents", "list", "--limit", "2", "--json").json()
    assert (page["total"], page["limit"], page["offset"]) == (3, 2, 0)
    assert [item["name"] for item in page["documents"]] == ["c.md", "b.md"]


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (["--limit", "0"], "limit must be between 1 and 100"),
        (["--limit", "101"], "limit must be between 1 and 100"),
        (["--offset", "-1"], "offset must be a non-negative integer"),
    ],
)
def test_documents_list_refuses_paging_outside_its_range(
    cli: Cli, options: list[str], message: str
) -> None:
    assert error_line(cli("documents", "list", *options, expect=2)) == message


def test_documents_get_finds_a_document_by_name_or_id_and_an_id_wins_over_a_name(
    cli: Cli, tmp_path: Path
) -> None:
    first_id = submit_id(cli, make_document(tmp_path / "one", "terms.md"))
    # The second document is named like the first one's ID.
    second_id = submit_id(cli, make_document(tmp_path / "two", "other.md"), "--name", first_id)

    by_name = cli("documents", "get", "terms.md")
    by_id = cli("documents", "get", first_id)
    assert by_name.err == "" and by_name.json() == by_id.json()
    shown = by_id.json()
    assert (shown["doc_id"], shown["name"], shown["kind"]) == (first_id, "terms.md", "document")
    assert shown["status"] == "completed"

    assert cli("documents", "get", second_id).json()["name"] == first_id


def test_documents_tree_prints_the_outline_with_node_ids_and_line_spans(
    cli: Cli, tmp_path: Path
) -> None:
    doc_id = submit_id(cli, make_document(tmp_path / "docs"))

    printed = cli("documents", "tree", "terms.md")
    lines = printed.out.splitlines()
    assert lines[0] == f"terms.md ({doc_id})"
    outline = re.compile(r"( +)(.+?)  \[([^\]]+)\]  lines (\d+)-(\d+)")
    parsed = [outline.fullmatch(line) for line in lines[1:]]
    entries = [match for match in parsed if match is not None]
    assert len(entries) == len(lines) - 1, lines
    # Two spaces of indent per level: "Late payment" is a subsection of "Payment terms".
    assert [(len(match[1]), match[2]) for match in entries] == [
        (2, "Invoice guide"),
        (4, "Payment terms"),
        (6, "Late payment"),
        (4, "Disputes"),
    ]
    payment = next(match for match in entries if match[2] == "Payment terms")
    assert (payment[4], payment[5]) == ("5", "12")
    assert cli("documents", "tree", doc_id).out == printed.out

    short = cli("documents", "tree", "terms.md", "--max-entries", "2").out.splitlines()
    assert len(short) == 4 and short[-1] == "(outline truncated)"

    structured = cli("documents", "tree", "terms.md", "--json").json()
    assert (structured["doc_id"], structured["name"], structured["truncated"]) == (
        doc_id,
        "terms.md",
        False,
    )
    assert [node["title"] for node in walk_nodes(structured["result"])] == [
        "Invoice guide",
        "Payment terms",
        "Late payment",
        "Disputes",
    ]


def test_documents_section_prints_one_section_and_its_json_carries_the_locator(
    cli: Cli, tmp_path: Path
) -> None:
    doc_id = submit_id(cli, make_document(tmp_path / "docs"))
    tree = cli("documents", "tree", "terms.md", "--json").json()
    node_id = find_node(tree, "Payment terms")["node_id"]

    printed = cli("documents", "section", "terms.md", node_id)
    heading, blank, *body = printed.out.splitlines()
    assert heading == "Payment terms - terms.md, lines 5-12" and blank == ""
    assert "Invoices are due net 30 days after receipt." in "\n".join(body)
    assert "\r" not in printed.out

    structured = cli("documents", "section", doc_id, node_id, "--json").json()
    assert (structured["doc_id"], structured["node_id"], structured["title"]) == (
        doc_id,
        node_id,
        "Payment terms",
    )
    assert structured["operation"] == "documents.get_section"
    assert structured["source_version"] == cli("documents", "get", doc_id).json()["version"]
    assert structured["locator"]["scheme"] == "markdown-heading"
    assert structured["locator"]["value"] == {
        "anchor": "payment-terms",
        "level": 2,
        "line_start": 5,
        "line_end": 12,
    }
    assert "Invoices are due net 30 days after receipt." in structured["text"]

    message = error_line(cli("documents", "section", "terms.md", "nope", expect=2))
    assert message == "'terms.md' has no section 'nope'; use get_tree to list its sections"


def test_documents_delete_forgets_the_document_and_never_touches_its_file(
    cli: Cli, tmp_path: Path
) -> None:
    source = make_document(tmp_path / "docs")
    content = source.read_bytes()
    doc_id = submit_id(cli, source)
    keeper_id = submit_id(cli, make_document(tmp_path / "docs", "keeper.md"))

    deleted = cli("documents", "delete", "terms.md")
    assert deleted.out == (
        f"Deleted terms.md ({doc_id}) from the workspace; the file itself was not changed.\n"
    )
    assert source.read_bytes() == content
    remaining = cli("documents", "list", "--json").json()["documents"]
    assert [item["doc_id"] for item in remaining] == [keeper_id]
    assert error_line(cli("documents", "delete", "terms.md", expect=2)) == (
        "no document has the ID or name 'terms.md'"
    )

    assert cli("documents", "delete", keeper_id).out.startswith("Deleted keeper.md (")
    assert cli("submit", str(source)).out.endswith("(new, 4 nodes)\n")


@pytest.mark.parametrize(
    "argv",
    [
        ["documents", "get", "ghost.md"],
        ["documents", "tree", "ghost.md"],
        ["documents", "section", "ghost.md", "some-node"],
        ["documents", "delete", "ghost.md"],
    ],
)
def test_an_unknown_document_reference_is_a_handled_error(
    cli: Cli, tmp_path: Path, argv: list[str]
) -> None:
    submit_id(cli, make_document(tmp_path / "docs"))

    assert error_line(cli(*argv, expect=2)) == "no document has the ID or name 'ghost.md'"


def test_an_unknown_reference_is_echoed_only_in_a_bounded_form(cli: Cli) -> None:
    echoed = error_line(cli("documents", "get", "x" * 200, expect=2))
    assert echoed == f"no document has the ID or name {'x' * 64!r}"

    # Past the name limit the reference is refused without being echoed at all.
    refused = error_line(cli("documents", "get", "x" * 5000, expect=2))
    assert refused == "name must be a non-empty string of at most 256 characters"


def test_a_changed_file_is_stale_everywhere_until_it_is_submitted_again(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    changed = make_document(tmp_path / "one", "terms.md")
    steady = make_document(tmp_path / "two", "steady.md")
    with serve(monkeypatch, document_policy(), model=MODEL) as server:
        changed_id = submit_id(cli, changed)
        steady_id = submit_id(cli, steady)
        changed.write_bytes(changed.read_bytes() + b"\r\nA clause added later.\r\n")

        listing = cli("documents", "list")
        rows = {line.split()[-1]: line.split() for line in listing.out.splitlines()[1:3]}
        assert rows["terms.md"][2] == "stale" and rows["steady.md"][2] == "completed"
        assert f"! terms.md: {STALE}" in listing.out
        assert cli("documents", "get", "terms.md").json()["status"] == "stale"

        # Naming the stale document is an error and never consults the model.
        for argv in (
            ["documents", "tree", "terms.md"],
            ["documents", "section", "terms.md", "any-node"],
            ["chat", QUESTION, "--doc", "terms.md", "--model", MODEL],
        ):
            assert error_line(cli(*argv, expect=2)).startswith(f"'terms.md': {STALE}")
        assert server.requests == []

        # Asking over everything skips it, answers from the rest and says so.
        answer = cli("chat", QUESTION, "--model", MODEL)
        assert answer.out.splitlines()[0] == f"{CLAIM} [1]"
        assert answer.err.startswith(f"dataindex: warning: skipped 'terms.md': {STALE}")
        assert answer.err.count("\n") == 1
        assert changed_id not in json.dumps(server.requests)
        structured = cli("chat", QUESTION, "--model", MODEL, "--json")
        assert structured.json()["warnings"] == [
            f"skipped 'terms.md': {STALE}; submit it again"
        ]
        assert structured.json()["citations"][0]["source_id"] == steady_id

        updated = cli("submit", str(changed), "--json").json()
        assert (updated["doc_id"], updated["change"]) == (changed_id, "updated")
        assert cli("documents", "get", "terms.md").json()["status"] == "completed"
        fresh = cli("chat", QUESTION, "--doc", "terms.md", "--model", MODEL)
        assert fresh.err == "" and fresh.out.startswith(CLAIM)


def test_when_every_document_is_stale_chat_abstains_without_a_model_call(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_document(tmp_path / "docs")
    with serve(monkeypatch, model=MODEL) as server:
        submit_id(cli, source)
        source.write_bytes(source.read_bytes() + b"\r\nChanged.\r\n")

        answer = cli("chat", QUESTION)

    assert answer.out == "None of the submitted documents could be read.\n"
    assert answer.err.startswith(f"dataindex: warning: skipped 'terms.md': {STALE}")
    assert server.requests == []


def test_a_missing_file_is_listed_as_missing_and_its_record_can_be_deleted(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_document(tmp_path / "docs")
    with serve(monkeypatch, model=MODEL) as server:
        submit_id(cli, source)
        source.unlink()

        listing = cli("documents", "list").out
        assert listing.splitlines()[1].split()[2] == "missing"
        assert "! terms.md: " in listing and "file is missing" in listing
        message = error_line(cli("chat", QUESTION, "--doc", "terms.md", expect=2))
        assert "file is missing" in message
        assert cli("documents", "delete", "terms.md").out.startswith("Deleted terms.md (")

    assert server.requests == []


# --- chat --------------------------------------------------------------------------


def test_chat_prints_a_cited_answer_and_a_numbered_source_list(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_document(tmp_path / "docs")
    with serve(monkeypatch, document_policy()) as server:
        doc_id = submit_id(cli, source)
        result = cli("chat", QUESTION, "--model", MODEL)

    assert result.err == ""
    lines = result.out.splitlines()
    assert lines[:3] == [f"{CLAIM} [1]", "", "Sources:"]
    assert re.fullmatch(
        rf"\[1\] terms\.md \({doc_id}, version sha256:[0-9a-f]+(\.\.\.)?\) - lines 5-12 "
        r"- documents\.search \(observed\)",
        lines[3],
    )
    assert len(lines) == 4

    assert len(server.requests) == 2 and server.errors == []
    assert {request["model"] for request in server.requests} == {MODEL}
    first = planner_payload(server.requests[0])
    assert first["question"] == QUESTION and "untrusted_data" in first
    everything = json.dumps(server.requests) + result.out + result.err
    assert not any(variant in everything for variant in path_variants(tmp_path))
    assert "Traceback" not in everything


def test_chat_json_prints_the_structured_answer_with_its_provenance(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_document(tmp_path / "docs")
    version = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    with serve(monkeypatch, document_policy()):
        doc_id = submit_id(cli, source)
        result = cli("chat", QUESTION, "--doc", "terms.md", "--model", MODEL, "--json")

    answer = result.json()
    assert result.err == ""
    assert answer["abstained"] is False and answer["reason"] is None
    assert answer["claims"] == [{"text": CLAIM, "citations": [1]}]
    assert answer["text"] == f"{CLAIM} [1]"
    (citation,) = answer["citations"]
    assert citation["source_id"] == doc_id and citation["source_version"] == version
    assert citation["document"] == "terms.md"
    assert (citation["operation"], citation["evidence_kind"]) == ("documents.search", "observed")
    assert citation["locator"]["scheme"] == "markdown-heading"
    assert citation["locator"]["value"]["line_start"] == 5
    assert answer["sources"] == [
        {"source_id": doc_id, "source_type": "text/markdown", "version": version}
    ]
    assert answer["warnings"] == []


def test_chat_over_a_database_cites_a_calculated_result_and_hides_the_machine(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = make_database(tmp_path / "dbs")
    with serve(monkeypatch, database_policy()) as server:
        db_id = submit_database_id(cli, tmp_path, database)
        human = cli("chat", DB_QUESTION, "--doc", "contracts.sqlite", "--model", MODEL)
        structured = cli("chat", DB_QUESTION, "--doc", db_id, "--model", MODEL, "--json")

    assert human.out.splitlines()[0] == f"{DB_CLAIM} [1]"
    assert re.search(
        rf"\[1\] contracts\.sqlite \({db_id}, version sqlite-state:[^)]*\) "
        r"- query invoices\.overdue_total_by_contract - databases\.query \(calculated\)",
        human.out,
    )
    answer = structured.json()
    # The model's claim states no figure: every number comes from the database evidence.
    assert not re.search(r"\d", "".join(claim["text"] for claim in answer["claims"]))
    (citation,) = answer["citations"]
    assert (citation["source_id"], citation["operation"]) == (db_id, "databases.query")
    assert citation["evidence_kind"] == "calculated"
    assert citation["locator"]["scheme"] == "sqlite-query"
    assert citation["locator"]["value"]["query_id"] == OVERDUE_QUERY_ID
    assert citation["position"] == f"query {OVERDUE_QUERY_ID}"
    # The JSON shows a clipped prefix of the calculated result, never the whole row set.
    excerpt = citation["excerpt"]
    assert excerpt.startswith(f'{{"query_id":"{OVERDUE_QUERY_ID}"') and len(excerpt) <= 400
    assert '"row_count":2' in excerpt and citation["excerpt_truncated"] is True

    sent = json.dumps(server.requests)
    assert not any(secret in sent for secret in (*path_variants(tmp_path), "tenant-a"))
    shown = human.out + human.err + structured.out + structured.err
    assert not any(secret in shown for secret in path_variants(tmp_path))
    assert "Traceback" not in shown


def test_chat_over_everything_offers_every_source_and_doc_narrows_it_by_name_or_id(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_document(tmp_path / "docs")
    database = make_database(tmp_path / "dbs")
    with serve(monkeypatch, document_policy(), model=MODEL) as server:
        doc_id = submit_id(cli, source)
        db_id = submit_database_id(cli, tmp_path, database)
        selections = [
            ([], {doc_id, db_id}),
            (["--doc", "terms.md"], {doc_id}),
            (["--doc", doc_id], {doc_id}),
            (["--doc", "contracts.sqlite"], {db_id}),
            (["--doc", "terms.md", "--doc", db_id], {doc_id, db_id}),
        ]
        for options, expected in selections:
            server.requests.clear()
            cli("chat", QUESTION, *options)
            offered = planner_payload(server.requests[0])["untrusted_data"]["sources"]
            assert {item["source_id"] for item in offered} == expected, options


def test_chat_with_an_unknown_doc_fails_before_any_model_call(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serve(monkeypatch, document_policy(), model=MODEL) as server:
        submit_id(cli, make_document(tmp_path / "docs"))
        message = error_line(cli("chat", QUESTION, "--doc", "terms.md", "--doc", "nope", expect=2))

    assert message == "no document has the ID or name 'nope'"
    assert server.requests == []


def test_the_model_comes_from_the_option_or_from_dataindex_model(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serve(monkeypatch, document_policy(), model="env-model") as server:
        submit_id(cli, make_document(tmp_path / "docs"))

        cli("chat", QUESTION)
        assert {request["model"] for request in server.requests} == {"env-model"}
        server.requests.clear()
        cli("chat", QUESTION, "--model", "option-model")
        assert {request["model"] for request in server.requests} == {"option-model"}


def test_chat_without_a_model_name_asks_for_one_and_calls_nothing(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serve(monkeypatch) as server:
        submit_id(cli, make_document(tmp_path / "docs"))

        assert error_line(cli("chat", QUESTION, expect=2)) == NO_MODEL
        assert error_line(cli("chat", QUESTION, "--model", "  ", expect=2)) == NO_MODEL
        assert error_line(cli("chat", stdin="a question\n", expect=2)) == NO_MODEL

    assert server.requests == []


def test_chat_without_documents_abstains_without_calling_the_model(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serve(monkeypatch, model=MODEL) as server:
        text = cli("chat", QUESTION)
        structured = cli("chat", QUESTION, "--json").json()

    assert text.out.startswith("No documents have been submitted yet") and text.err == ""
    assert structured["abstained"] is True
    assert structured["claims"] == [] and structured["citations"] == []
    assert server.requests == []


@pytest.mark.parametrize("question", ["", "   ", "x" * 2049])
def test_chat_refuses_an_empty_or_oversized_question_without_a_model_call(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, question: str
) -> None:
    with serve(monkeypatch, document_policy(), model=MODEL) as server:
        submit_id(cli, make_document(tmp_path / "docs"))

        message = error_line(cli("chat", question, expect=2))

    assert message == "question must be a string of 1 to 2048 characters"
    assert server.requests == []


def test_instructions_reach_only_the_system_message_and_are_bounded(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    note = "Answer in one sentence."
    with serve(monkeypatch, document_policy(), model=MODEL) as server:
        submit_id(cli, make_document(tmp_path / "docs"))
        cli("chat", QUESTION, "--instructions", note)
        used = list(server.requests)
        server.requests.clear()

        message = error_line(cli("chat", QUESTION, "--instructions", "x" * 4001, expect=2))

    assert message == "instructions must be at most 4000 characters"
    assert server.requests == []
    assert used
    for body in used:
        assert system_prompt(body).startswith(SYSTEM_PROMPT)
        assert note in system_prompt(body)
        assert note not in json.dumps(planner_payload(body))


# --- the interactive loop ----------------------------------------------------------


def test_the_chat_loop_answers_each_question_on_its_own_until_exit(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = "And the late fees?"
    with serve(monkeypatch, document_policy(), model=MODEL) as server:
        submit_id(cli, make_document(tmp_path / "docs"))

        piped = f"{QUESTION}\n\n   \n{other}\nEXIT\nnever asked\n"
        result = cli("chat", "--doc", "terms.md", stdin=piped)

    assert result.err == ""
    assert result.out.count("Sources:") == 2
    assert [planner_payload(body)["question"] for body in server.requests] == [
        QUESTION,
        QUESTION,
        other,
        other,
    ]
    # Each question starts with no evidence: nothing carries over between them.
    starts = [planner_payload(body) for body in server.requests[::2]]
    assert all(payload["untrusted_data"]["evidence"] == [] for payload in starts)
    assert "never asked" not in json.dumps(server.requests)


@pytest.mark.parametrize(
    "stdin",
    [QUESTION, f"{QUESTION}\n", f"{QUESTION}\nquit\nignored\n", f"\n\n{QUESTION}\n  Quit  \n"],
    ids=["end of input", "trailing newline", "quit", "blank lines and padded quit"],
)
def test_the_chat_loop_ends_at_quit_or_at_the_end_of_input(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdin: str
) -> None:
    with serve(monkeypatch, document_policy(), model=MODEL):
        submit_id(cli, make_document(tmp_path / "docs"))

        result = cli("chat", stdin=stdin)

    assert result.err == "" and result.out.count("Sources:") == 1


@pytest.mark.parametrize("stdin", ["", "exit\n", "\n  \n", "QUIT"])
def test_the_chat_loop_asks_nothing_when_no_question_is_given(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdin: str
) -> None:
    with serve(monkeypatch, document_policy(), model=MODEL) as server:
        submit_id(cli, make_document(tmp_path / "docs"))

        result = cli("chat", stdin=stdin)

    assert (result.out, result.err) == ("", "")
    assert server.requests == []


def test_the_chat_loop_prints_one_json_object_per_question(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serve(monkeypatch, document_policy(), model=MODEL):
        doc_id = submit_id(cli, make_document(tmp_path / "docs"))

        result = cli("chat", "--json", stdin=f"{QUESTION}\nWhat else?\n")

    answers = result.json_objects()
    assert result.err == "" and len(answers) == 2
    for answer in answers:
        assert answer["abstained"] is False
        assert {citation["source_id"] for citation in answer["citations"]} == {doc_id}


def test_an_interactive_session_greets_on_stderr_prompts_and_ends_at_exit(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serve(monkeypatch, document_policy(), model=MODEL):
        submit_id(cli, make_document(tmp_path / "docs"))
        monkeypatch.setattr(sys, "stdin", Terminal(f"{QUESTION}\nexit\n"))

        session = cli("chat")

    assert session.err == "Ask about your documents. Type exit to quit.\n"
    assert session.out.startswith(f"> {CLAIM} [1]\n")
    assert session.out.endswith("\n\n> ")  # a blank line after the answer, then the next prompt


def test_ctrl_c_at_the_prompt_ends_the_session_with_status_130_and_no_traceback(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serve(monkeypatch, document_policy(), model=MODEL) as server:
        submit_id(cli, make_document(tmp_path / "docs"))
        monkeypatch.setattr(sys, "stdin", InterruptedTerminal())

        session = cli("chat", expect=130)

    assert session.out == "> " and "Traceback" not in session.err
    assert server.requests == []


def test_a_failing_question_does_not_stop_the_chat_loop(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planner = planner_responder(document_policy())

    def flaky(body: Any) -> str:
        if planner_payload(body)["question"] == "explode":
            raise RuntimeError("internal detail that must stay private")
        return planner(body)

    with serve_raw(monkeypatch, flaky, model=MODEL) as server:
        submit_id(cli, make_document(tmp_path / "docs"))

        result = cli("chat", stdin=f"explode\n{QUESTION}\n", expect=2)

    assert result.out.count("Sources:") == 1  # the second question was still answered
    assert result.err.startswith("dataindex: the chat endpoint returned HTTP 500")
    assert result.err.count("\n") == 1
    assert "internal detail" not in result.out + result.err
    assert len(server.requests) == 3


# --- failures and hostile text -----------------------------------------------------


def test_a_failing_chat_endpoint_is_a_handled_error_without_a_traceback(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(body: Any) -> str:
        raise RuntimeError("internal detail that must stay private")

    with serve_raw(monkeypatch, broken, model=MODEL) as server:
        submit_id(cli, make_document(tmp_path / "docs"))

        message = error_line(cli("chat", QUESTION, expect=2))

    assert message.startswith("the chat endpoint returned HTTP 500")
    assert "internal detail" not in message
    assert len(server.requests) == 1
    assert not any(variant in message for variant in path_variants(tmp_path))


def test_a_reply_that_is_not_json_gets_one_repair_request_then_an_error(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serve_raw(monkeypatch, lambda body: "I would rather chat.", model=MODEL) as server:
        submit_id(cli, make_document(tmp_path / "docs"))

        message = error_line(cli("chat", QUESTION, expect=2))

    assert message == "the reply is not a single JSON object"
    assert len(server.requests) == 2
    assert not is_repair(server.requests[0]) and is_repair(server.requests[1])


def test_terminal_control_characters_are_replaced_in_text_and_escaped_in_json(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dirty = "Invoices are due net 30 days.\x1b[2J\u202e after receipt."
    with serve(monkeypatch, document_policy(dirty), model=MODEL):
        submit_id(cli, make_document(tmp_path / "docs"))

        human = cli("chat", QUESTION)
        structured = cli("chat", QUESTION, "--json")

    assert "\x1b" not in human.out and "\u202e" not in human.out
    assert human.out.splitlines()[0] == "Invoices are due net 30 days.?[2J? after receipt. [1]"
    assert "\x1b" not in structured.out and "\\u001b" in structured.out
    assert structured.json()["claims"][0]["text"] == dirty


def test_the_api_key_from_the_environment_is_sent_only_as_a_bearer_header(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serve(monkeypatch, document_policy(), model=MODEL) as server:
        monkeypatch.setenv("OPENAI_API_KEY", API_KEY)
        submit_id(cli, make_document(tmp_path / "docs"))

        result = cli("chat", QUESTION)

    assert server.requests
    assert {headers["authorization"] for headers in server.headers} == {f"Bearer {API_KEY}"}
    assert API_KEY not in json.dumps(server.requests) + result.out + result.err


def test_a_provider_error_never_shows_the_api_key_or_terminal_control_characters(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def rejecting(body: Any) -> str:
        raise HttpFailure(401, f"Incorrect API key {API_KEY}: \x1b[31mdenied\u202e")

    with serve_raw(monkeypatch, rejecting, model=MODEL) as server:
        monkeypatch.setenv("OPENAI_API_KEY", API_KEY)
        submit_id(cli, make_document(tmp_path / "docs"))

        message = error_line(cli("chat", QUESTION, expect=2))

    assert message.startswith("the chat endpoint returned HTTP 401")
    assert API_KEY not in message
    assert not any(character in message for character in "\x1b\u202e")
    assert len(server.requests) == 1  # a rejected key is not retried


def test_a_remote_endpoint_without_an_api_key_is_refused_before_any_request(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    submit_id(cli, make_document(tmp_path / "docs"))
    monkeypatch.setenv("OPENAI_BASE_URL", "https://llm.example.invalid/v1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    message = error_line(cli("chat", QUESTION, "--model", MODEL, expect=2))

    assert message.startswith("no API key is configured; set the OPENAI_API_KEY")


def test_a_document_that_attacks_its_reader_cannot_steer_the_cli(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    injected = write_injection_document(tmp_path / "docs")
    database = make_database(tmp_path / "dbs")
    # A model that obeys the document and cites the evidence ID it asks for.
    obedient = scripted(answer_reply((CLAIM, ["e99"])))
    with serve(monkeypatch, obedient, model=MODEL) as server:
        doc_id = submit_id(cli, injected)
        db_id = submit_database_id(cli, tmp_path, database)

        refused = cli("chat", QUESTION, "--doc", "injected.md", expect=2)

        message = error_line(refused)
        assert message.startswith("claim cites unknown evidence IDs ['e99']")
        assert len(server.requests) == 2  # the first reply and one repair, never a retry loop
        for body in server.requests:
            payload = planner_payload(body)
            assert system_prompt(body) == SYSTEM_PROMPT
            untrusted = json.dumps(payload["untrusted_data"])
            assert INJECTION_MARKERS[1] in untrusted
            outside = system_prompt(body) + payload["question"] + json.dumps(payload["state"])
            assert not any(marker in outside for marker in INJECTION_MARKERS)
            offered = {item["source_id"] for item in payload["untrusted_data"]["sources"]}
            assert offered == {doc_id}
            assert db_id not in json.dumps(body)

        # Reading the section shows the hostile text as plain data and runs nothing.
        server.requests.clear()
        tree = cli("documents", "tree", "injected.md", "--json").json()
        node_id = find_node(tree, "Payment terms")["node_id"]
        section = cli("documents", "section", "injected.md", node_id)
        assert section.code == 0 and INJECTION_MARKERS[0] in section.out
        assert server.requests == []


# --- usage and workspace ----------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["documents"],
        ["submit"],
        ["submit-db", "contracts.sqlite"],
        ["documents", "section", "only-a-document"],
        ["chat", "--bogus"],
    ],
    ids=["no documents action", "no path", "no --config", "no node id", "unknown option"],
)
def test_a_usage_error_exits_with_status_2_and_prints_usage_to_stderr(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stop:
        main(argv)

    captured = capsys.readouterr()
    assert stop.value.code == 2
    assert captured.out == "" and "usage:" in captured.err


def test_each_storage_directory_is_a_separate_workspace(cli: Cli, tmp_path: Path) -> None:
    source = make_document(tmp_path / "docs")
    other = tmp_path / "elsewhere" / "workspace"

    submit_id(cli, source)
    assert cli.storage.is_dir()
    assert cli("documents", "list", "--json", storage=other).json()["total"] == 0
    assert other.is_dir()  # created on first use
    assert cli("documents", "list", "--json").json()["total"] == 1


def test_without_storage_the_workspace_is_dot_dataindex_in_the_current_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source = make_document(tmp_path / "docs")

    assert main(["submit", str(source)]) == 0
    capsys.readouterr()
    assert main(["documents", "list", "--json"]) == 0

    assert (tmp_path / ".dataindex").is_dir()
    assert json.loads(capsys.readouterr().out)["total"] == 1
