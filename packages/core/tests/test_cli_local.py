from __future__ import annotations

import io
import json
import shutil
import sqlite3
import sys
import types
import warnings
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

import pytest

from dataindex.answers import ChatAnswer
from dataindex.cli import main
from dataindex.cli_local import printable
from dataindex.exceptions import ConfigurationError
from dataindex.llm import ChatResult
from dataindex.local import content_version
from dataindex.sdk import DataIndexClient
from dataindex.workspace import WorkspaceDocument, WorkspaceRegistry

Result = tuple[int, str, str]

EMPTY_LISTING_HINT = (
    "No documents have been submitted. Add one with: dataindex submit PATH"
)
NO_DOCUMENTS_REASON = (
    "No documents have been submitted yet; submit a document first."
)
DOCUMENTS_HINT = "pip install dataindex-ext-documents"
DATABASES_HINT = "pip install dataindex-ext-databases"
VALID_DATABASE_CONFIG = json.dumps(
    {"allowed_columns": {"orders": ["id", "total"]}, "queries": [{"query_id": "q1"}]}
)


class RecordingModel:
    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self,
        messages: Sequence[object],
        *,
        max_output_tokens: int,
        timeout_seconds: float,
        json_output: bool = True,
    ) -> ChatResult:
        self.calls += 1
        return ChatResult('{"action": "answer", "claims": []}')


class FakeLlmPackage:
    """Stands in for the optional dataindex_llm package."""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.model = RecordingModel()

    def create_chat_model(self, name: str, **options: str) -> RecordingModel:
        self.created.append(name)
        return self.model

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeLlmPackage:
        module = types.ModuleType("dataindex_llm")
        setattr(module, "create_chat_model", self.create_chat_model)
        monkeypatch.setitem(sys.modules, "dataindex_llm", module)
        return self


class FakeTerminal(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A stray default ./.dataindex lands in tmp_path, never in the repository.
    monkeypatch.chdir(tmp_path)
    for name in ("DATAINDEX_MODEL", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("dataindex.local.entry_points", lambda *, group: ())
    monkeypatch.setattr("dataindex.registry.entry_points", lambda *, group: ())


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    return tmp_path / "ws"


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> FakeLlmPackage:
    return FakeLlmPackage().install(monkeypatch)


@pytest.fixture
def database_file(tmp_path: Path) -> Path:
    path = tmp_path / "orders.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, total REAL)")
        connection.commit()
    finally:
        connection.close()
    return path


def write_file(
    directory: Path, name: str, content: str = "# Heading\n\nBody.\n"
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


def row_for(
    doc_id: str, name: str, path: Path, version: str | None = None
) -> WorkspaceDocument:
    return WorkspaceDocument(
        doc_id,
        name,
        "document",
        "text/markdown",
        "documents",
        str(path),
        content_version(path.read_bytes()) if version is None else version,
    )


def seed(storage: Path, *rows: WorkspaceDocument) -> None:
    storage.mkdir(parents=True, exist_ok=True)
    with WorkspaceRegistry(storage / "workspace.sqlite3") as registry:
        for row in rows:
            registry.upsert(row)


@pytest.fixture
def seeded(tmp_path: Path, ws: Path) -> dict[str, WorkspaceDocument]:
    """One document per readiness state that exists without any plugin."""
    files = tmp_path / "files"
    ok = row_for("doc-ok", "ok-notes.md", write_file(files, "ok-notes.md"))
    stale_path = write_file(files, "stale-notes.md")
    stale = row_for("doc-stale", "stale-notes.md", stale_path)
    stale_path.write_text("# Changed\n", encoding="utf-8")
    missing = row_for(
        "doc-missing",
        "missing-notes.md",
        files / "missing-notes.md",
        version=f"sha256:{'0' * 64}",
    )
    seed(ws, ok, stale, missing)
    return {"ok": ok, "stale": stale, "missing": missing}


def invoke(capsys: pytest.CaptureFixture[str], *argv: str) -> Result:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def assert_clean_failure(result: Result, *fragments: str) -> None:
    code, out, err = result
    assert code == 2, err
    assert out == ""
    assert err.startswith("dataindex: ")
    assert "Traceback" not in err
    for fragment in fragments:
        assert fragment in err


def decode_all(text: str) -> list[object]:
    decoder = json.JSONDecoder()
    documents: list[object] = []
    position = 0
    while position < len(text):
        if text[position].isspace():
            position += 1
            continue
        document, position = decoder.raw_decode(text, position)
        documents.append(document)
    return documents


# --- printable ----------------------------------------------------------------

CONTROL_CHARACTERS = [
    chr(code)
    for code in (
        *range(0x00, 0x09),
        *range(0x0B, 0x20),
        *range(0x7F, 0xA0),
    )
]
BIDI_CHARACTERS = [
    chr(code) for code in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))
]


def test_printable_turns_crlf_into_lf_and_keeps_tab_and_newline() -> None:
    assert printable("a\r\nb\tc\nd") == "a\nb\tc\nd"


def test_printable_replaces_control_characters_including_escape_and_lone_cr() -> None:
    for character in CONTROL_CHARACTERS:
        assert printable(f"a{character}b") == "a?b", hex(ord(character))
    assert printable("a\rb") == "a?b"
    assert printable("\x1b[2J\x1b]0;title\x07") == "?[2J?]0;title?"


def test_printable_replaces_bidirectional_overrides_and_isolates() -> None:
    for character in BIDI_CHARACTERS:
        assert printable(f"a{character}b") == "a?b", hex(ord(character))


def test_printable_leaves_ordinary_text_alone() -> None:
    text = "Zahlung in 30 Tagen - café 日本語 😀 [1] {x} 100%"
    assert printable(text) == text
    assert printable("") == ""


# --- argument errors and help -----------------------------------------------

BAD_ARGUMENTS = [
    pytest.param([], id="no-command"),
    pytest.param(["frobnicate"], id="unknown-command"),
    pytest.param(["submit"], id="submit-without-path"),
    pytest.param(["submit", "a.md", "b.md"], id="submit-two-paths"),
    pytest.param(["submit-db", "data.sqlite"], id="submit-db-without-config"),
    pytest.param(["documents"], id="documents-without-action"),
    pytest.param(["documents", "list", "--limit", "many"], id="limit-not-a-number"),
    pytest.param(["documents", "list", "--offset", "1.5"], id="offset-not-an-integer"),
    pytest.param(["documents", "get"], id="get-without-document"),
    pytest.param(["documents", "section", "doc-1"], id="section-without-node"),
    pytest.param(
        ["documents", "tree", "doc-1", "--max-entries", "x"],
        id="max-entries-not-a-number",
    ),
    pytest.param(["documents", "delete", "doc-1", "extra"], id="delete-extra-argument"),
    pytest.param(["chat", "one", "two"], id="chat-two-questions"),
    pytest.param(["chat", "--doc"], id="doc-without-value"),
    pytest.param(["chat", "--model"], id="model-without-value"),
]


@pytest.mark.parametrize("argv", BAD_ARGUMENTS)
def test_bad_arguments_exit_with_code_2_and_create_nothing(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, argv: list[str]
) -> None:
    with pytest.raises(SystemExit) as caught:
        main(argv)
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert "usage:" in err
    assert "error:" in err
    assert list(tmp_path.iterdir()) == []


def test_help_lists_the_local_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--help"])
    assert caught.value.code == 0
    out = capsys.readouterr().out
    for command in ("submit", "submit-db", "documents", "chat"):
        assert command in out


def test_chat_help_names_the_model_option_and_environment_variable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["chat", "--help"])
    assert caught.value.code == 0
    out = capsys.readouterr().out
    for fragment in ("--model", "--doc", "--instructions", "DATAINDEX_MODEL"):
        assert fragment in out


# --- documents list ------------------------------------------------------------


def test_documents_list_on_an_empty_workspace_prints_the_hint_and_creates_storage(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, ws: Path
) -> None:
    code, out, err = invoke(capsys, "documents", "list", "--storage", str(ws))
    assert (code, out, err) == (0, EMPTY_LISTING_HINT + "\n", "")
    assert (ws / "catalog.sqlite3").is_file()
    assert (ws / "workspace.sqlite3").is_file()
    assert not (tmp_path / ".dataindex").exists()


def test_the_default_storage_is_dot_dataindex_in_the_working_directory(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code, out, _ = invoke(capsys, "documents", "list")
    assert code == 0
    assert "No documents have been submitted" in out
    assert (tmp_path / ".dataindex" / "workspace.sqlite3").is_file()


def test_documents_list_json_on_an_empty_workspace_has_a_stable_shape(
    capsys: pytest.CaptureFixture[str], ws: Path
) -> None:
    code, out, err = invoke(capsys, "documents", "list", "--json", "--storage", str(ws))
    assert (code, err) == (0, "")
    assert json.loads(out) == {"documents": [], "total": 0, "limit": 50, "offset": 0}

    _, out, _ = invoke(
        capsys,
        "documents",
        "list",
        "--json",
        "--limit",
        "5",
        "--offset",
        "2",
        "--storage",
        str(ws),
    )
    assert json.loads(out) == {"documents": [], "total": 0, "limit": 5, "offset": 2}


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [
        ("--limit", "0", "limit must be between 1 and 100"),
        ("--limit", "101", "limit must be between 1 and 100"),
        ("--limit", "-5", "limit must be between 1 and 100"),
        ("--offset", "-1", "offset must be a non-negative integer"),
    ],
)
def test_documents_list_rejects_out_of_range_paging(
    capsys: pytest.CaptureFixture[str], ws: Path, option: str, value: str, message: str
) -> None:
    result = invoke(capsys, "documents", "list", option, value, "--storage", str(ws))
    assert_clean_failure(result, message)


def test_documents_list_shows_newest_first_with_status_and_detail_lines(
    capsys: pytest.CaptureFixture[str], ws: Path, seeded: dict[str, WorkspaceDocument]
) -> None:
    code, out, err = invoke(capsys, "documents", "list", "--storage", str(ws))
    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert lines[0].split() == ["ID", "KIND", "STATUS", "NAME"]
    assert [line.split() for line in lines[1:4]] == [
        ["doc-missing", "document", "missing", "missing-notes.md"],
        ["doc-stale", "document", "stale", "stale-notes.md"],
        ["doc-ok", "document", "unavailable", "ok-notes.md"],
    ]
    assert (
        "! missing-notes.md: file is missing; restore it or delete the document"
        in lines
    )
    assert any(
        line.startswith("! stale-notes.md: file changed after it was submitted")
        for line in lines
    )
    assert (
        f"! ok-notes.md: the 'documents' plugin is not installed; run: {DOCUMENTS_HINT}"
        in lines
    )
    assert not any(line.startswith("Showing") for line in lines)


def test_documents_list_pages_and_says_how_to_continue(
    capsys: pytest.CaptureFixture[str], ws: Path, seeded: dict[str, WorkspaceDocument]
) -> None:
    _, out, _ = invoke(
        capsys, "documents", "list", "--limit", "2", "--storage", str(ws)
    )
    assert "missing-notes.md" in out and "stale-notes.md" in out
    assert "ok-notes.md" not in out
    assert "Showing 1-2 of 3; use --offset 2 for more." in out

    _, out, _ = invoke(
        capsys,
        "documents",
        "list",
        "--limit",
        "2",
        "--offset",
        "2",
        "--storage",
        str(ws),
    )
    assert "doc-ok" in out and "doc-stale" not in out
    assert "Showing" not in out

    _, out, _ = invoke(
        capsys, "documents", "list", "--offset", "10", "--storage", str(ws)
    )
    assert out == "No documents at offset 10; the workspace holds 3.\n"


def test_documents_list_json_reports_each_documents_state(
    capsys: pytest.CaptureFixture[str], ws: Path, seeded: dict[str, WorkspaceDocument]
) -> None:
    _, out, _ = invoke(capsys, "documents", "list", "--json", "--storage", str(ws))
    listing = json.loads(out)
    assert listing["total"] == 3
    assert [(item["doc_id"], item["status"]) for item in listing["documents"]] == [
        ("doc-missing", "missing"),
        ("doc-stale", "stale"),
        ("doc-ok", "unavailable"),
    ]


def test_hostile_document_names_cannot_inject_terminal_controls(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, ws: Path
) -> None:
    hostile = "evil\x9b[31m\u202egpj.md"
    seed(ws, row_for("doc-evil", hostile, write_file(tmp_path / "files", "evil.md")))
    _, out, err = invoke(capsys, "documents", "list", "--storage", str(ws))
    assert "evil?[31m?gpj.md" in out
    for text in (out, err):
        assert "\x9b" not in text and "\u202e" not in text


def test_unencodable_names_become_question_marks_on_an_ascii_console(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ws: Path
) -> None:
    seed(ws, row_for("doc-cafe", "café.md", write_file(tmp_path / "files", "cafe.md")))
    buffer = io.BytesIO()
    console = io.TextIOWrapper(buffer, encoding="ascii", errors="strict")
    monkeypatch.setattr(sys, "stdout", console)
    assert main(["documents", "list", "--storage", str(ws)]) == 0
    console.flush()
    assert "caf?.md" in buffer.getvalue().decode("ascii")


# --- documents get / tree / section / delete ---------------------------------

LOOKUP_COMMANDS = [
    pytest.param(["documents", "get"], id="get"),
    pytest.param(["documents", "tree"], id="tree"),
    pytest.param(["documents", "section"], id="section"),
    pytest.param(["documents", "delete"], id="delete"),
]


def lookup(command: list[str], reference: str, ws: Path) -> list[str]:
    extra = ["node-1"] if command[-1] == "section" else []
    return [*command, reference, *extra, "--storage", str(ws)]


@pytest.mark.parametrize("command", LOOKUP_COMMANDS)
def test_an_unknown_document_is_a_clean_error(
    capsys: pytest.CaptureFixture[str], ws: Path, command: list[str]
) -> None:
    result = invoke(capsys, *lookup(command, "nope", ws))
    assert_clean_failure(result, "no document has the ID or name 'nope'")


@pytest.mark.parametrize("command", LOOKUP_COMMANDS)
def test_an_unknown_document_reference_is_clipped_in_the_error(
    capsys: pytest.CaptureFixture[str], ws: Path, command: list[str]
) -> None:
    result = invoke(capsys, *lookup(command, "r" * 200, ws))
    assert_clean_failure(result, "no document has the ID or name")
    assert "r" * 64 in result[2]
    assert "r" * 65 not in result[2]


@pytest.mark.parametrize(
    ("reference", "message"),
    [
        pytest.param("", "doc_id must be a non-empty string", id="empty"),
        pytest.param("r" * 300, "name", id="longer-than-any-name"),
        pytest.param("bad\x1b[2Jname", "control characters", id="control-characters"),
    ],
)
def test_malformed_document_references_are_rejected_without_echoing_them(
    capsys: pytest.CaptureFixture[str], ws: Path, reference: str, message: str
) -> None:
    result = invoke(capsys, "documents", "get", reference, "--storage", str(ws))
    assert_clean_failure(result, message)
    assert "\x1b" not in result[2]
    assert "r" * 65 not in result[2]


def test_an_unknown_reference_with_a_bidi_override_is_escaped_in_the_error(
    capsys: pytest.CaptureFixture[str], ws: Path
) -> None:
    result = invoke(capsys, "documents", "get", "x\u202ey", "--storage", str(ws))
    assert_clean_failure(result, "no document has the ID or name")
    assert "\u202e" not in result[2]


def test_documents_get_prints_the_document_as_json_by_id_or_by_name(
    capsys: pytest.CaptureFixture[str], ws: Path, seeded: dict[str, WorkspaceDocument]
) -> None:
    for reference in ("doc-stale", "stale-notes.md"):
        code, out, err = invoke(
            capsys, "documents", "get", reference, "--storage", str(ws)
        )
        assert (code, err) == (0, "")
        info = json.loads(out)
        assert (info["doc_id"], info["name"], info["status"]) == (
            "doc-stale",
            "stale-notes.md",
            "stale",
        )


def test_an_id_wins_over_a_name(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, ws: Path
) -> None:
    files = tmp_path / "files"
    seed(
        ws,
        row_for("doc-aaa", "doc-bbb", write_file(files, "a.md")),
        row_for("doc-bbb", "other.md", write_file(files, "b.md")),
    )
    _, out, _ = invoke(capsys, "documents", "get", "doc-bbb", "--storage", str(ws))
    assert json.loads(out)["doc_id"] == "doc-bbb"
    _, out, _ = invoke(capsys, "documents", "get", "other.md", "--storage", str(ws))
    assert json.loads(out)["doc_id"] == "doc-bbb"


@pytest.mark.parametrize("command", ["tree", "section"])
@pytest.mark.parametrize(
    ("reference", "message"),
    [
        pytest.param("doc-missing", "file is missing", id="missing-file"),
        pytest.param("missing-notes.md", "file is missing", id="missing-file-by-name"),
        pytest.param("doc-stale", "changed after it was submitted", id="changed-file"),
        pytest.param("doc-ok", DOCUMENTS_HINT, id="plugin-not-installed"),
    ],
)
def test_reading_an_unreadable_document_explains_why(
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    seeded: dict[str, WorkspaceDocument],
    command: str,
    reference: str,
    message: str,
) -> None:
    result = invoke(capsys, *lookup(["documents", command], reference, ws))
    assert_clean_failure(result, message)


@pytest.mark.parametrize("value", ["0", "-3"])
def test_documents_tree_rejects_a_non_positive_entry_limit(
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    seeded: dict[str, WorkspaceDocument],
    value: str,
) -> None:
    result = invoke(
        capsys,
        "documents",
        "tree",
        "doc-ok",
        "--max-entries",
        value,
        "--storage",
        str(ws),
    )
    assert_clean_failure(result, "max_entries must be a positive integer")


def test_documents_section_rejects_an_overlong_node_id(
    capsys: pytest.CaptureFixture[str], ws: Path, seeded: dict[str, WorkspaceDocument]
) -> None:
    result = invoke(
        capsys, "documents", "section", "doc-ok", "n" * 129, "--storage", str(ws)
    )
    assert_clean_failure(result, "node_id must be a string of 1 to 128 characters")
    assert "n" * 65 not in result[2]


def span(start: int, end: int) -> dict[str, object]:
    return {
        "scheme": "text-lines",
        "version": 1,
        "value": {"line_start": start, "line_end": end},
    }


TREE_RESULT: dict[str, object] = {
    "doc_id": "doc-ok",
    "name": "ok\u202e-notes.md",
    "status": "completed",
    "truncated": True,
    "result": [
        {
            "node_id": "n1",
            "title": "Intro",
            "locator": span(1, 9),
            "nodes": [
                {
                    "node_id": "n2",
                    "title": "Scope \x1b[31m",
                    "locator": span(3, 5),
                    "nodes": [],
                }
            ],
        },
        {"node_id": "n3", "title": "Terms", "nodes": []},
    ],
}


def test_documents_tree_prints_an_indented_outline_with_line_spans(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    seeded: dict[str, WorkspaceDocument],
) -> None:
    requested: list[tuple[str, int | None]] = []

    def get_tree(
        self: DataIndexClient, doc_id: str, *, max_entries: int | None = None
    ) -> dict[str, object]:
        requested.append((doc_id, max_entries))
        return TREE_RESULT

    monkeypatch.setattr(DataIndexClient, "get_tree", get_tree)
    command = ["documents", "tree", "ok-notes.md", "--storage", str(ws)]

    code, out, err = invoke(capsys, *command, "--max-entries", "7")
    assert (code, err) == (0, "")
    assert out == (
        "ok?-notes.md (doc-ok)\n"
        "  Intro  [n1]  lines 1-9\n"
        "    Scope ?[31m  [n2]  lines 3-5\n"
        "  Terms  [n3]\n"
        "(outline truncated)\n"
    )
    invoke(capsys, *command)
    assert requested == [("doc-ok", 7), ("doc-ok", None)]


def test_documents_tree_json_prints_the_result_as_escaped_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    seeded: dict[str, WorkspaceDocument],
) -> None:
    monkeypatch.setattr(
        DataIndexClient, "get_tree", lambda self, doc_id, **_: TREE_RESULT
    )
    code, out, err = invoke(
        capsys, "documents", "tree", "doc-ok", "--json", "--storage", str(ws)
    )
    assert (code, err) == (0, "")
    assert json.loads(out) == TREE_RESULT
    assert "\x1b" not in out and "\u202e" not in out


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        pytest.param("not a list", "ok-notes.md (doc-ok)\n", id="result-not-a-list"),
        pytest.param(
            ["junk", 5, None, {"node_id": "n1", "title": "Kept", "nodes": "junk"}],
            "ok-notes.md (doc-ok)\n  Kept  [n1]\n",
            id="entries-not-objects",
        ),
        pytest.param(
            [
                {"node_id": "n1", "title": "A", "locator": "x", "nodes": []},
                {
                    "node_id": "n2",
                    "title": "B",
                    "locator": {"value": {"line_start": True, "line_end": 2}},
                    "nodes": [],
                },
                {
                    "node_id": "n3",
                    "title": "C",
                    "locator": {"value": {"line_start": 1}},
                },
            ],
            "ok-notes.md (doc-ok)\n  A  [n1]\n  B  [n2]\n  C  [n3]\n",
            id="locators-without-line-spans",
        ),
    ],
)
def test_documents_tree_tolerates_malformed_outline_data(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    seeded: dict[str, WorkspaceDocument],
    result: object,
    expected: str,
) -> None:
    outline = {
        "doc_id": "doc-ok",
        "name": "ok-notes.md",
        "truncated": False,
        "result": result,
    }
    monkeypatch.setattr(DataIndexClient, "get_tree", lambda self, doc_id, **_: outline)
    code, out, err = invoke(capsys, "documents", "tree", "doc-ok", "--storage", str(ws))
    assert (code, out, err) == (0, expected, "")


SECTION_RESULT: dict[str, object] = {
    "doc_id": "doc-ok",
    "name": "ok-notes.md",
    "node_id": "n9",
    "title": "Payment\u202e",
    "locator": span(12, 20),
    "text": "Pay in 30 days.\x1b[2J\r\nSecond line",
}


def test_documents_section_prints_a_heading_and_neutralized_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    seeded: dict[str, WorkspaceDocument],
) -> None:
    requested: list[tuple[str, str]] = []

    def get_section(
        self: DataIndexClient, doc_id: str, node_id: str
    ) -> dict[str, object]:
        requested.append((doc_id, node_id))
        return SECTION_RESULT

    monkeypatch.setattr(DataIndexClient, "get_section", get_section)
    code, out, err = invoke(
        capsys, "documents", "section", "ok-notes.md", "n9", "--storage", str(ws)
    )
    assert (code, err) == (0, "")
    assert requested == [("doc-ok", "n9")]
    assert (
        out
        == "Payment? - ok-notes.md, lines 12-20\n\nPay in 30 days.?[2J\nSecond line\n"
    )


def test_documents_section_json_prints_the_result_as_escaped_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    seeded: dict[str, WorkspaceDocument],
) -> None:
    monkeypatch.setattr(
        DataIndexClient, "get_section", lambda self, doc_id, node_id: SECTION_RESULT
    )
    code, out, err = invoke(
        capsys, "documents", "section", "doc-ok", "n9", "--json", "--storage", str(ws)
    )
    assert (code, err) == (0, "")
    assert json.loads(out) == SECTION_RESULT
    assert "\x1b" not in out and "\u202e" not in out


def test_json_output_escapes_hostile_names_so_they_round_trip(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, ws: Path
) -> None:
    hostile = "evil\x9b[31m\u202egpj.md"
    seed(ws, row_for("doc-evil", hostile, write_file(tmp_path / "files", "evil.md")))
    got = invoke(capsys, "documents", "get", "doc-evil", "--storage", str(ws))[1]
    listed = invoke(capsys, "documents", "list", "--json", "--storage", str(ws))[1]
    assert json.loads(got)["name"] == hostile
    assert json.loads(listed)["documents"][0]["name"] == hostile
    for out in (got, listed):
        assert "\x9b" not in out and "\u202e" not in out


def test_documents_delete_neutralizes_the_name_it_reports(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, ws: Path
) -> None:
    hostile = "evil\x9b[31m\u202egpj.md"
    seed(ws, row_for("doc-evil", hostile, write_file(tmp_path / "files", "evil.md")))
    code, out, _ = invoke(
        capsys, "documents", "delete", "doc-evil", "--storage", str(ws)
    )
    assert code == 0
    assert out.startswith("Deleted evil?[31m?gpj.md (doc-evil) from the workspace;")


def test_documents_delete_removes_only_the_workspace_entry(
    capsys: pytest.CaptureFixture[str], ws: Path, seeded: dict[str, WorkspaceDocument]
) -> None:
    file = Path(seeded["ok"].path)
    before = file.read_bytes()

    code, out, err = invoke(
        capsys, "documents", "delete", "ok-notes.md", "--storage", str(ws)
    )
    assert (code, err) == (0, "")
    assert out == (
        "Deleted ok-notes.md (doc-ok) from the workspace; "
        "the file itself was not changed.\n"
    )
    assert file.read_bytes() == before

    assert_clean_failure(
        invoke(capsys, "documents", "get", "doc-ok", "--storage", str(ws)),
        "no document has the ID or name 'doc-ok'",
    )
    assert_clean_failure(
        invoke(capsys, "documents", "delete", "doc-ok", "--storage", str(ws)),
        "no document has the ID or name",
    )
    _, out, _ = invoke(capsys, "documents", "list", "--json", "--storage", str(ws))
    assert json.loads(out)["total"] == 2


# --- submit ------------------------------------------------------------------------


def test_submit_of_a_missing_file_is_a_clean_error_and_adds_nothing(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, ws: Path
) -> None:
    result = invoke(capsys, "submit", str(tmp_path / "nofile.md"), "--storage", str(ws))
    assert_clean_failure(result, "nofile.md")
    _, out, _ = invoke(capsys, "documents", "list", "--storage", str(ws))
    assert out == EMPTY_LISTING_HINT + "\n"


def test_submit_of_a_directory_is_refused(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, ws: Path
) -> None:
    (tmp_path / "folder").mkdir()
    result = invoke(capsys, "submit", str(tmp_path / "folder"), "--storage", str(ws))
    assert_clean_failure(result, "is not a regular file")


@pytest.mark.parametrize(
    ("name", "message"),
    [
        pytest.param("photo.png", "unsupported file type '.png'", id="png"),
        pytest.param("SETUP.EXE", "unsupported file type '.exe'", id="exe-upper-case"),
        pytest.param("bundle.zip", "unsupported file type '.zip'", id="zip"),
        pytest.param(
            "README", "unsupported file type '(no extension)'", id="no-extension"
        ),
    ],
)
def test_submit_refuses_unsupported_file_types(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    ws: Path,
    name: str,
    message: str,
) -> None:
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\n not really")
    assert_clean_failure(
        invoke(capsys, "submit", str(path), "--storage", str(ws)), message
    )


@pytest.mark.parametrize(
    "name",
    ["orders.sqlite", "orders.sqlite3", "orders.db", "ORDERS.DB", "orders.duckdb"],
)
def test_submit_points_database_files_to_submit_db(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, ws: Path, name: str
) -> None:
    path = tmp_path / name
    path.write_bytes(b"SQLite format 3\x00")
    result = invoke(capsys, "submit", str(path), "--storage", str(ws))
    assert_clean_failure(
        result, "is a database file", "dataindex submit-db PATH --config FILE"
    )


def test_submit_without_any_plugin_gives_the_install_hint(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, ws: Path
) -> None:
    path = write_file(tmp_path, "notes.md")
    result = invoke(capsys, "submit", str(path), "--storage", str(ws))
    assert_clean_failure(
        result, "the 'documents' plugin is not installed", DOCUMENTS_HINT
    )
    _, out, _ = invoke(capsys, "documents", "list", "--json", "--storage", str(ws))
    assert json.loads(out)["total"] == 0


def test_submit_refuses_a_file_over_the_size_limit(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, ws: Path
) -> None:
    path = tmp_path / "big.md"
    path.write_bytes(b"a" * (5 * 1024 * 1024 + 1))
    assert_clean_failure(
        invoke(capsys, "submit", str(path), "--storage", str(ws)), "5 MiB"
    )


@pytest.mark.parametrize("name", ["", "two\nlines", "esc\x1b[2J"])
def test_submit_validates_the_display_name_first(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, ws: Path, name: str
) -> None:
    path = write_file(tmp_path, "notes.md")
    result = invoke(capsys, "submit", str(path), "--name", name, "--storage", str(ws))
    assert_clean_failure(result, "name")
    assert DOCUMENTS_HINT not in result[2]


def test_submit_prints_a_summary_line_or_the_json_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    ws: Path,
) -> None:
    outcome = {
        "doc_id": "doc-1",
        "name": "notes.md",
        "status": "completed",
        "change": "new",
        "source_type": "text/markdown",
        "version": "sha256:abc",
        "node_count": 3,
    }
    monkeypatch.setattr(
        DataIndexClient,
        "submit_document",
        lambda self, path, *, name=None, wait=True: outcome,
    )
    path = write_file(tmp_path, "notes.md")

    code, out, err = invoke(capsys, "submit", str(path), "--storage", str(ws))
    assert (code, out, err) == (0, "Submitted notes.md as doc-1 (new, 3 nodes)\n", "")

    code, out, err = invoke(capsys, "submit", str(path), "--json", "--storage", str(ws))
    assert (code, err) == (0, "")
    assert json.loads(out) == outcome


def test_submit_hands_the_file_and_display_name_to_the_client(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    ws: Path,
) -> None:
    received: list[tuple[object, str | None]] = []

    def submit_document(
        self: DataIndexClient,
        path: object,
        *,
        name: str | None = None,
        wait: bool = True,
    ) -> dict[str, object]:
        received.append((path, name))
        return {
            "doc_id": "doc-1",
            "name": name or "notes.md",
            "change": "new",
            "node_count": 1,
        }

    monkeypatch.setattr(DataIndexClient, "submit_document", submit_document)
    path = write_file(tmp_path, "notes.md")
    assert invoke(capsys, "submit", str(path), "--storage", str(ws))[:2] == (
        0,
        "Submitted notes.md as doc-1 (new, 1 nodes)\n",
    )
    assert invoke(
        capsys, "submit", str(path), "--name", "Q3 report", "--storage", str(ws)
    )[:2] == (
        0,
        "Submitted Q3 report as doc-1 (new, 1 nodes)\n",
    )
    assert received == [(path, None), (path, "Q3 report")]


@pytest.mark.parametrize("fails", [False, True], ids=["success", "failure"])
def test_library_warnings_are_printed_to_stderr_even_when_the_command_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    ws: Path,
    fails: bool,
) -> None:
    def submit_document(
        self: DataIndexClient,
        path: object,
        *,
        name: str | None = None,
        wait: bool = True,
    ) -> dict[str, object]:
        warnings.warn("a document named 'x' already exists\x1b[2J", UserWarning)
        if fails:
            raise ConfigurationError("could not index it")
        return {"doc_id": "doc-1", "name": "x_1", "change": "new", "node_count": 1}

    monkeypatch.setattr(DataIndexClient, "submit_document", submit_document)
    path = write_file(tmp_path, "notes.md")
    code, out, err = invoke(capsys, "submit", str(path), "--storage", str(ws))
    warning = "dataindex: warning: a document named 'x' already exists?[2J"
    assert warning in err
    assert "\x1b" not in err
    if fails:
        assert code == 2
        assert err.index(warning) < err.index("dataindex: could not index it")
    else:
        assert code == 0
        assert out.startswith("Submitted x_1 as doc-1")


# --- submit-db ---------------------------------------------------------------------


def write_config(tmp_path: Path, content: str | bytes) -> Path:
    path = tmp_path / "config.json"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def submit_db(
    capsys: pytest.CaptureFixture[str], database: Path, config: Path, ws: Path
) -> Result:
    return invoke(
        capsys,
        "submit-db",
        str(database),
        "--config",
        str(config),
        "--storage",
        str(ws),
    )


def test_submit_db_points_other_files_to_submit(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, ws: Path
) -> None:
    notes = write_file(tmp_path, "notes.md")
    config = write_config(tmp_path, VALID_DATABASE_CONFIG)
    result = submit_db(capsys, notes, config, ws)
    assert_clean_failure(result, "is not a database file", "dataindex submit PATH")


def test_submit_db_without_any_plugin_gives_the_install_hint(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, ws: Path, database_file: Path
) -> None:
    config = write_config(tmp_path, VALID_DATABASE_CONFIG)
    result = submit_db(capsys, database_file, config, ws)
    assert_clean_failure(
        result, "the 'databases' plugin is not installed", DATABASES_HINT
    )


def test_submit_db_with_a_missing_or_unreadable_config_is_a_clean_error(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, ws: Path, database_file: Path
) -> None:
    assert_clean_failure(
        submit_db(capsys, database_file, tmp_path / "absent.json", ws), "absent.json"
    )
    folder = tmp_path / "folder.json"
    folder.mkdir()
    assert_clean_failure(submit_db(capsys, database_file, folder, ws), "folder.json")


BAD_CONFIGS = [
    pytest.param("not json", "is not valid JSON", id="not-json"),
    pytest.param("", "is not valid JSON", id="empty-file"),
    pytest.param(b"\x80\x81\x82", "is not valid JSON", id="binary-garbage"),
    pytest.param(
        "[" * 120_000 + "]" * 120_000, "is not valid JSON", id="deeply-nested"
    ),
    pytest.param("[]", "must be a JSON object", id="array"),
    pytest.param('"text"', "must be a JSON object", id="string"),
    pytest.param("42", "must be a JSON object", id="number"),
    pytest.param("null", "must be a JSON object", id="null"),
    pytest.param("{}", "must be a JSON object", id="empty-object"),
    pytest.param(
        '{"allowed_columns": {"t": ["a"]}}', "no other keys", id="missing-queries"
    ),
    pytest.param('{"queries": []}', "no other keys", id="missing-allowed-columns"),
    pytest.param(
        '{"allowed_columns": {"t": ["a"]}, "queries": [], "extra": 1}',
        "no other keys",
        id="extra-key",
    ),
    pytest.param(
        '{"allowed_columns": [], "queries": []}',
        "'allowed_columns' object",
        id="columns-list",
    ),
    pytest.param(
        '{"allowed_columns": {}, "queries": {}}', "'queries' list", id="queries-object"
    ),
    pytest.param(
        '{"allowed_columns": {}, "queries": []}',
        "allowed_columns must map at least one table",
        id="empty-approval",
    ),
    pytest.param(
        '{"allowed_columns": {"t": ["a"]}, "queries": []}',
        "queries must be a non-empty list",
        id="no-queries",
    ),
    pytest.param(
        '{"allowed_columns": {"t": "a"}, "queries": [{"query_id": "q"}]}',
        "must be a list of column names",
        id="columns-not-a-list",
    ),
    pytest.param(
        '{"allowed_columns": {"t": ["a"]}, "queries": [{"name": "q"}]}',
        "string query_id",
        id="query-without-id",
    ),
    pytest.param(
        '{"allowed_columns": {"t": ["a"]}, "queries": ["q"]}',
        "each query must be",
        id="query-not-an-object",
    ),
    pytest.param(
        '{"allowed_columns": {"t": ["a"]}, "queries": [{"query_id": "q", "x": NaN}]}',
        "must be finite",
        id="non-finite-number",
    ),
]


@pytest.mark.parametrize(("content", "message"), BAD_CONFIGS)
def test_submit_db_rejects_a_bad_config_without_a_traceback(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    ws: Path,
    database_file: Path,
    content: str | bytes,
    message: str,
) -> None:
    result = submit_db(capsys, database_file, write_config(tmp_path, content), ws)
    assert_clean_failure(result, message)
    assert DATABASES_HINT not in result[2]
    assert len(result[2]) < 400


def test_submit_db_refuses_a_config_over_256_kib(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, ws: Path, database_file: Path
) -> None:
    config = write_config(tmp_path, " " * (256 * 1024 + 1))
    result = submit_db(capsys, database_file, config, ws)
    assert_clean_failure(result, "is larger than 256 KiB")
    assert not ws.exists()


def test_submit_db_reads_a_config_of_exactly_256_kib(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, ws: Path, database_file: Path
) -> None:
    padding = " " * (256 * 1024 - len(VALID_DATABASE_CONFIG))
    config = write_config(tmp_path, VALID_DATABASE_CONFIG + padding)
    result = submit_db(capsys, database_file, config, ws)
    assert_clean_failure(result, DATABASES_HINT)


def test_submit_db_hands_the_parsed_config_and_name_to_the_client(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    ws: Path,
    database_file: Path,
) -> None:
    received: dict[str, object] = {}

    def submit_database(
        self: DataIndexClient,
        path: object,
        *,
        allowed_columns: object,
        queries: object,
        name: str | None = None,
    ) -> dict[str, object]:
        received.update(
            path=path, allowed_columns=allowed_columns, queries=queries, name=name
        )
        return {"doc_id": "db-1", "name": "Orders", "change": "new", "node_count": 4}

    monkeypatch.setattr(DataIndexClient, "submit_database", submit_database)
    config = write_config(tmp_path, VALID_DATABASE_CONFIG)
    code, out, err = invoke(
        capsys,
        "submit-db",
        str(database_file),
        "--config",
        str(config),
        "--name",
        "Orders",
        "--storage",
        str(ws),
    )
    assert (code, out, err) == (0, "Submitted Orders as db-1 (new, 4 nodes)\n", "")
    assert received == {
        "path": database_file,
        "allowed_columns": {"orders": ["id", "total"]},
        "queries": [{"query_id": "q1"}],
        "name": "Orders",
    }


SERVER_URL = "postgresql+psycopg://reader@db.example.com:5432/sales"


def test_submit_db_hands_a_server_connection_to_the_client(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    ws: Path,
) -> None:
    received: dict[str, object] = {}

    def submit_database(
        self: DataIndexClient, path: object = None, **arguments: object
    ) -> dict[str, object]:
        received.update(arguments, path=path)
        return {"doc_id": "db-2", "name": "Sales", "change": "new", "node_count": 3}

    monkeypatch.setattr(DataIndexClient, "submit_database", submit_database)
    config = write_config(tmp_path, VALID_DATABASE_CONFIG)
    code, out, err = invoke(
        capsys,
        "submit-db",
        "--url",
        SERVER_URL,
        "--password-env",
        "DATAINDEX_SALES_PASSWORD",
        "--schema",
        "public",
        "--config",
        str(config),
        "--storage",
        str(ws),
    )
    assert (code, out, err) == (0, "Submitted Sales as db-2 (new, 3 nodes)\n", "")
    assert received == {
        "path": None,
        "url": SERVER_URL,
        "password_env": "DATAINDEX_SALES_PASSWORD",
        "schema": "public",
        "allowed_columns": {"orders": ["id", "total"]},
        "queries": [{"query_id": "q1"}],
        "name": None,
    }


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        pytest.param([], "exactly one of a database file PATH or --url", id="neither"),
        pytest.param(
            ["{database}", "--url", SERVER_URL],
            "exactly one of a database file PATH or --url",
            id="both",
        ),
        pytest.param(
            ["{database}", "--password-env", "DATAINDEX_X"],
            "--password-env and --schema need --url",
            id="password-env-without-url",
        ),
        pytest.param(
            ["{database}", "--schema", "public"],
            "--password-env and --schema need --url",
            id="schema-without-url",
        ),
    ],
)
def test_submit_db_needs_exactly_one_source(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    ws: Path,
    database_file: Path,
    arguments: list[str],
    message: str,
) -> None:
    config = write_config(tmp_path, VALID_DATABASE_CONFIG)
    argv = [item.replace("{database}", str(database_file)) for item in arguments]
    result = invoke(
        capsys, "submit-db", *argv, "--config", str(config), "--storage", str(ws)
    )
    assert_clean_failure(result, message)
    assert not ws.exists()


# --- chat: model selection -----------------------------------------------------


@pytest.mark.parametrize(
    ("environment", "option"),
    [
        pytest.param(None, None, id="nothing-set"),
        pytest.param("   ", None, id="blank-environment"),
        pytest.param(None, "", id="empty-option"),
        pytest.param("env-model", "   ", id="blank-option-beats-the-environment"),
    ],
)
def test_chat_without_a_model_says_how_to_choose_one(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    llm: FakeLlmPackage,
    environment: str | None,
    option: str | None,
) -> None:
    if environment is not None:
        monkeypatch.setenv("DATAINDEX_MODEL", environment)
    argv = ["chat", "What is due?", "--storage", str(ws)]
    if option is not None:
        argv += ["--model", option]
    assert_clean_failure(invoke(capsys, *argv), "--model", "DATAINDEX_MODEL")
    assert llm.created == []
    assert not ws.exists()


def test_chat_takes_its_model_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    llm: FakeLlmPackage,
) -> None:
    monkeypatch.setenv("DATAINDEX_MODEL", "  env-model  ")
    code, out, err = invoke(capsys, "chat", "What is due?", "--storage", str(ws))
    assert (code, out, err) == (0, NO_DOCUMENTS_REASON + "\n", "")
    assert llm.created == ["env-model"]


def test_the_model_option_beats_the_environment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    llm: FakeLlmPackage,
) -> None:
    monkeypatch.setenv("DATAINDEX_MODEL", "env-model")
    code, _, _ = invoke(
        capsys, "chat", "What is due?", "--model", "flag-model", "--storage", str(ws)
    )
    assert code == 0
    assert llm.created == ["flag-model"]


def test_chat_with_a_model_name_but_no_adapter_package_says_what_to_install(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ws: Path,
) -> None:
    monkeypatch.setitem(sys.modules, "dataindex_llm", None)
    result = invoke(
        capsys, "chat", "What is due?", "--model", "any-model", "--storage", str(ws)
    )
    assert_clean_failure(result, "dataindex-llm")
    assert not ws.exists()


# --- chat: a single question --------------------------------------------------


def test_chat_over_an_empty_workspace_abstains_without_calling_the_model(
    capsys: pytest.CaptureFixture[str], ws: Path, llm: FakeLlmPackage
) -> None:
    code, out, err = invoke(
        capsys, "chat", "What is due?", "--model", "fake-model", "--storage", str(ws)
    )
    assert (code, out, err) == (0, NO_DOCUMENTS_REASON + "\n", "")
    assert llm.model.calls == 0


def test_chat_json_prints_only_the_structured_answer(
    capsys: pytest.CaptureFixture[str], ws: Path, llm: FakeLlmPackage
) -> None:
    code, out, err = invoke(
        capsys,
        "chat",
        "What is due?",
        "--json",
        "--model",
        "fake-model",
        "--storage",
        str(ws),
    )
    assert (code, err) == (0, "")
    answer = json.loads(out)
    assert answer["abstained"] is True
    assert answer["reason"] == NO_DOCUMENTS_REASON
    assert (answer["claims"], answer["citations"], answer["sources"]) == ([], [], [])
    assert answer["warnings"] == []
    assert llm.model.calls == 0


@pytest.mark.parametrize(
    "question",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param("x" * 2049, id="too-long"),
    ],
)
def test_chat_rejects_a_bad_question(
    capsys: pytest.CaptureFixture[str], ws: Path, llm: FakeLlmPackage, question: str
) -> None:
    result = invoke(
        capsys, "chat", question, "--model", "fake-model", "--storage", str(ws)
    )
    assert_clean_failure(result, "question must be a string of 1 to 2048 characters")
    assert "x" * 65 not in result[2]
    assert llm.model.calls == 0


def test_chat_accepts_a_question_of_2048_characters(
    capsys: pytest.CaptureFixture[str], ws: Path, llm: FakeLlmPackage
) -> None:
    code, out, _ = invoke(
        capsys, "chat", "q" * 2048, "--model", "fake-model", "--storage", str(ws)
    )
    assert (code, out) == (0, NO_DOCUMENTS_REASON + "\n")


def test_chat_rejects_overlong_instructions_before_creating_anything(
    capsys: pytest.CaptureFixture[str], ws: Path, llm: FakeLlmPackage
) -> None:
    result = invoke(
        capsys,
        "chat",
        "What is due?",
        "--model",
        "fake-model",
        "--instructions",
        "y" * 4001,
        "--storage",
        str(ws),
    )
    assert_clean_failure(result, "4000")
    assert llm.created == []
    assert not ws.exists()


def test_chat_accepts_blank_instructions(
    capsys: pytest.CaptureFixture[str], ws: Path, llm: FakeLlmPackage
) -> None:
    code, _, _ = invoke(
        capsys,
        "chat",
        "What is due?",
        "--model",
        "fake-model",
        "--instructions",
        "   ",
        "--storage",
        str(ws),
    )
    assert code == 0


def test_a_question_argument_means_stdin_is_never_read(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    llm: FakeLlmPackage,
) -> None:
    stdin = io.StringIO("one\ntwo\n")
    monkeypatch.setattr("sys.stdin", stdin)
    code, out, _ = invoke(
        capsys, "chat", "What is due?", "--model", "fake-model", "--storage", str(ws)
    )
    assert code == 0
    assert out.count(NO_DOCUMENTS_REASON) == 1
    assert stdin.read() == "one\ntwo\n"


def test_chat_with_an_unknown_document_fails_before_asking(
    capsys: pytest.CaptureFixture[str], ws: Path, llm: FakeLlmPackage
) -> None:
    result = invoke(
        capsys,
        "chat",
        "What is due?",
        "--model",
        "fake-model",
        "--doc",
        "nope",
        "--storage",
        str(ws),
    )
    assert_clean_failure(result, "no document has the ID or name 'nope'")
    assert llm.model.calls == 0


@pytest.mark.parametrize(
    ("reference", "message"),
    [
        pytest.param(
            "doc-missing", "'missing-notes.md': file is missing", id="missing-by-id"
        ),
        pytest.param("missing-notes.md", "file is missing", id="missing-by-name"),
        pytest.param("doc-stale", "changed after it was submitted", id="changed"),
        pytest.param(
            "doc-ok",
            f"'ok-notes.md': the 'documents' plugin is not installed; run: {DOCUMENTS_HINT}",
            id="plugin-by-id",
        ),
        pytest.param("ok-notes.md", DOCUMENTS_HINT, id="plugin-by-name"),
    ],
)
def test_chat_fails_when_a_document_chosen_with_doc_cannot_be_read(
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    llm: FakeLlmPackage,
    seeded: dict[str, WorkspaceDocument],
    reference: str,
    message: str,
) -> None:
    result = invoke(
        capsys,
        "chat",
        "What is due?",
        "--model",
        "fake-model",
        "--doc",
        reference,
        "--storage",
        str(ws),
    )
    assert_clean_failure(result, message)
    assert llm.model.calls == 0


def test_chat_passes_the_chosen_documents_to_the_client_as_ids(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    llm: FakeLlmPackage,
    seeded: dict[str, WorkspaceDocument],
) -> None:
    chosen: list[object] = []

    def chat(
        self: DataIndexClient,
        question: str,
        doc_id: object = None,
        *,
        budget: object = None,
        instructions: object = None,
    ) -> ChatAnswer:
        chosen.append(doc_id)
        return ChatAnswer("fine")

    monkeypatch.setattr(DataIndexClient, "chat", chat)
    command = ["chat", "What is due?", "--model", "fake-model", "--storage", str(ws)]
    assert invoke(capsys, *command)[:2] == (0, "fine\n")
    invoke(capsys, *command, "--doc", "ok-notes.md", "--doc", "doc-stale")
    assert chosen == [None, ["doc-ok", "doc-stale"]]


def test_chat_skips_unreadable_documents_with_warnings_instead_of_guessing(
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    llm: FakeLlmPackage,
    seeded: dict[str, WorkspaceDocument],
) -> None:
    code, out, err = invoke(
        capsys, "chat", "What is due?", "--model", "fake-model", "--storage", str(ws)
    )
    assert code == 0
    assert out == "None of the submitted documents could be read.\n"
    warning_lines = [line for line in err.splitlines() if line]
    assert len(warning_lines) == 3
    assert all(
        line.startswith("dataindex: warning: skipped ") for line in warning_lines
    )
    assert any("missing-notes.md" in line for line in warning_lines)
    assert llm.model.calls == 0


def test_chat_json_carries_the_skipped_document_warnings(
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    llm: FakeLlmPackage,
    seeded: dict[str, WorkspaceDocument],
) -> None:
    code, out, err = invoke(
        capsys,
        "chat",
        "What is due?",
        "--json",
        "--model",
        "fake-model",
        "--storage",
        str(ws),
    )
    assert (code, err) == (0, "")
    answer = json.loads(out)
    assert answer["abstained"] is True
    assert len(answer["warnings"]) == 3


# --- chat: output is neutralized ---------------------------------------------


def stub_answer(monkeypatch: pytest.MonkeyPatch, answer: ChatAnswer) -> None:
    monkeypatch.setattr(
        DataIndexClient,
        "chat",
        lambda self, question, doc_id=None, *, budget=None, instructions=None: answer,
    )


def test_answer_text_and_warnings_are_neutralized_in_text_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    llm: FakeLlmPackage,
) -> None:
    stub_answer(
        monkeypatch,
        ChatAnswer(
            "pay \x1b[2J in 30\u202e days\r\nend", warnings=("skipped \x1b[31mdoc",)
        ),
    )
    code, out, err = invoke(
        capsys, "chat", "What is due?", "--model", "fake-model", "--storage", str(ws)
    )
    assert code == 0
    assert out == "pay ?[2J in 30? days\nend\n"
    assert err == "dataindex: warning: skipped ?[31mdoc\n"


def test_json_mode_keeps_the_text_and_lets_json_escape_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    llm: FakeLlmPackage,
) -> None:
    stub_answer(monkeypatch, ChatAnswer("pay \x1b[2J \u202e end", warnings=("w\x1b",)))
    code, out, err = invoke(
        capsys,
        "chat",
        "What is due?",
        "--json",
        "--model",
        "fake-model",
        "--storage",
        str(ws),
    )
    assert (code, err) == (0, "")
    assert "\x1b" not in out and "\u202e" not in out
    assert json.loads(out)["text"] == "pay \x1b[2J \u202e end"


# --- chat: reading questions from standard input ---------------------------------


def converse(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    stdin_text: str,
    *extra: str,
) -> Result:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    return invoke(capsys, "chat", "--model", "fake-model", "--storage", str(ws), *extra)


def test_piped_questions_are_answered_one_by_one_and_blank_lines_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    llm: FakeLlmPackage,
) -> None:
    code, out, err = converse(monkeypatch, capsys, ws, "\n   \nfirst\n\n\t\nsecond\n")
    assert (code, err) == (0, "")
    assert out == (NO_DOCUMENTS_REASON + "\n") * 2


@pytest.mark.parametrize("word", ["exit", "quit", "EXIT", "Quit", "  exit  "])
def test_exit_and_quit_end_the_loop_in_any_case(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    llm: FakeLlmPackage,
    word: str,
) -> None:
    code, out, _ = converse(monkeypatch, capsys, ws, f"first\n{word}\nsecond\n")
    assert code == 0
    assert out.count(NO_DOCUMENTS_REASON) == 1


@pytest.mark.parametrize(
    ("stdin_text", "answers"),
    [
        pytest.param("first\nsecond", 2, id="no-trailing-newline"),
        pytest.param("first\nsecond\n", 2, id="trailing-newline"),
        pytest.param("", 0, id="empty-input"),
        pytest.param("\n\n  \n", 0, id="only-blank-lines"),
    ],
)
def test_end_of_input_ends_the_loop(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    llm: FakeLlmPackage,
    stdin_text: str,
    answers: int,
) -> None:
    code, out, err = converse(monkeypatch, capsys, ws, stdin_text)
    assert (code, err) == (0, "")
    assert out.count(NO_DOCUMENTS_REASON) == answers
    assert llm.model.calls == 0


def test_a_failing_question_does_not_stop_the_loop_but_the_exit_code_is_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    llm: FakeLlmPackage,
) -> None:
    code, out, err = converse(
        monkeypatch, capsys, ws, "first\n" + "x" * 2049 + "\nsecond\n"
    )
    assert code == 2
    assert out.count(NO_DOCUMENTS_REASON) == 2
    assert err == "dataindex: question must be a string of 1 to 2048 characters\n"


def test_errors_inside_the_loop_are_neutralized(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    llm: FakeLlmPackage,
) -> None:
    def chat(
        self: DataIndexClient, question: str, doc_id: object = None, **_: object
    ) -> ChatAnswer:
        raise ConfigurationError("bad \x1b[31m red\u202e text\r\nline2")

    monkeypatch.setattr(DataIndexClient, "chat", chat)
    code, out, err = converse(monkeypatch, capsys, ws, "first\n")
    assert (code, out) == (2, "")
    assert err == "dataindex: bad ?[31m red? text\nline2\n"


def test_json_mode_prints_one_json_document_per_answer(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    llm: FakeLlmPackage,
) -> None:
    code, out, err = converse(monkeypatch, capsys, ws, "first\nsecond\n", "--json")
    assert (code, err) == (0, "")
    answers = decode_all(out)
    assert len(answers) == 2
    assert all(isinstance(item, dict) and item["abstained"] is True for item in answers)


# --- chat: an interactive terminal ---------------------------------------------


def type_lines(monkeypatch: pytest.MonkeyPatch, *lines: str) -> None:
    pending = iter(lines)

    def fake_input(prompt: str = "") -> str:
        try:
            return next(pending)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("sys.stdin", FakeTerminal())
    monkeypatch.setattr("builtins.input", fake_input)


def test_a_terminal_session_shows_a_banner_and_a_blank_line_after_each_answer(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    llm: FakeLlmPackage,
) -> None:
    type_lines(monkeypatch, "What is due?", "", "exit", "never asked")
    code, out, err = invoke(
        capsys, "chat", "--model", "fake-model", "--storage", str(ws)
    )
    assert code == 0
    assert err == "Ask about your documents. Type exit to quit.\n"
    assert out == NO_DOCUMENTS_REASON + "\n\n"


def test_end_of_input_at_the_prompt_ends_a_terminal_session_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    llm: FakeLlmPackage,
) -> None:
    type_lines(monkeypatch, "What is due?")
    code, out, _ = invoke(capsys, "chat", "--model", "fake-model", "--storage", str(ws))
    assert code == 0
    assert out.count(NO_DOCUMENTS_REASON) == 1
    assert out.endswith("\n\n")


def test_a_terminal_session_in_json_mode_prints_no_blank_lines(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    llm: FakeLlmPackage,
) -> None:
    type_lines(monkeypatch, "What is due?", "exit")
    code, out, _ = invoke(
        capsys, "chat", "--json", "--model", "fake-model", "--storage", str(ws)
    )
    assert code == 0
    assert out.endswith("}\n")
    assert len(decode_all(out)) == 1


# --- interruption and unexpected failures --------------------------------------


def test_interrupting_a_terminal_session_exits_130_and_releases_the_storage(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    llm: FakeLlmPackage,
) -> None:
    def interrupt(prompt: str = "") -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("sys.stdin", FakeTerminal())
    monkeypatch.setattr("builtins.input", interrupt)
    code, out, err = invoke(
        capsys, "chat", "--model", "fake-model", "--storage", str(ws)
    )
    assert (code, out) == (130, "")
    assert "Traceback" not in err
    shutil.rmtree(ws)


@pytest.mark.parametrize(
    ("method", "argv"),
    [
        pytest.param("list_documents", ["documents", "list"], id="documents-list"),
        pytest.param("get_document", ["documents", "get", "doc-1"], id="documents-get"),
        pytest.param(
            "chat", ["chat", "What is due?", "--model", "fake-model"], id="chat"
        ),
    ],
)
def test_ctrl_c_during_a_command_exits_130_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    llm: FakeLlmPackage,
    method: str,
    argv: list[str],
) -> None:
    def interrupt(self: DataIndexClient, *args: object, **kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(DataIndexClient, method, interrupt)
    code, out, err = invoke(capsys, *argv, "--storage", str(ws))
    assert (code, out) == (130, "")
    assert "Traceback" not in err
    shutil.rmtree(ws)


def test_ctrl_c_during_submit_exits_130(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    ws: Path,
) -> None:
    def interrupt(self: DataIndexClient, *args: object, **kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(DataIndexClient, "submit_document", interrupt)
    path = write_file(tmp_path, "notes.md")
    code, out, err = invoke(capsys, "submit", str(path), "--storage", str(ws))
    assert (code, out) == (130, "")
    assert "Traceback" not in err
    shutil.rmtree(ws)


@pytest.mark.parametrize(
    ("handler", "argv"),
    [
        pytest.param("_extensions", ["extensions", "list"], id="catalog-commands"),
        pytest.param("run_local_command", ["documents", "list"], id="local-commands"),
    ],
)
def test_ctrl_c_is_reported_the_same_way_for_every_command_family(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    handler: str,
    argv: list[str],
) -> None:
    def interrupt(args: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(f"dataindex.cli.{handler}", interrupt)
    assert invoke(capsys, *argv) == (130, "", "\n")


HOSTILE_MESSAGE = "bad \x1b[31m red\u202e text\r\nline2"


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ConfigurationError(HOSTILE_MESSAGE), id="dataindex-error"),
        pytest.param(OSError(HOSTILE_MESSAGE), id="os-error"),
        pytest.param(TimeoutError(HOSTILE_MESSAGE), id="timeout"),
        pytest.param(ValueError(HOSTILE_MESSAGE), id="value-error"),
    ],
)
def test_expected_errors_are_neutralized_before_they_reach_the_terminal(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    error: Exception,
) -> None:
    def fail(self: DataIndexClient, *args: object, **kwargs: object) -> object:
        raise error

    monkeypatch.setattr(DataIndexClient, "list_documents", fail)
    code, out, err = invoke(capsys, "documents", "list", "--storage", str(ws))
    assert (code, out) == (2, "")
    assert err == "dataindex: bad ?[31m red? text\nline2\n"
    shutil.rmtree(ws)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(RuntimeError("a bug"), id="runtime-error"),
        pytest.param(KeyError("a bug"), id="key-error"),
        pytest.param(AssertionError("a bug"), id="assertion-error"),
    ],
)
def test_unexpected_exceptions_are_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, ws: Path, error: Exception
) -> None:
    def fail(self: DataIndexClient, *args: object, **kwargs: object) -> object:
        raise error

    monkeypatch.setattr(DataIndexClient, "list_documents", fail)
    with pytest.raises(type(error)):
        main(["documents", "list", "--storage", str(ws)])
    shutil.rmtree(ws)


# --- damaged or unusable storage ----------------------------------------------------


def test_a_corrupt_workspace_file_is_reported_and_left_untouched(
    capsys: pytest.CaptureFixture[str], ws: Path
) -> None:
    ws.mkdir()
    damaged = b"this is not a database" * 50
    (ws / "workspace.sqlite3").write_bytes(damaged)
    result = invoke(capsys, "documents", "list", "--storage", str(ws))
    assert_clean_failure(result, "could not read the workspace registry", "corrupt")
    assert (ws / "workspace.sqlite3").read_bytes() == damaged
    shutil.rmtree(ws)


@pytest.mark.parametrize(
    ("file_name", "fragments"),
    [
        pytest.param(
            "workspace.sqlite3",
            ("workspace schema 99 is newer", "upgrade dataindex-core"),
            id="workspace",
        ),
        pytest.param("catalog.sqlite3", ("catalog schema 99 is newer",), id="catalog"),
    ],
)
def test_storage_written_by_a_newer_dataindex_is_refused_cleanly(
    capsys: pytest.CaptureFixture[str],
    ws: Path,
    file_name: str,
    fragments: tuple[str, ...],
) -> None:
    ws.mkdir()
    with closing(sqlite3.connect(ws / file_name)) as connection:
        connection.execute("PRAGMA user_version = 99")
    result = invoke(capsys, "documents", "list", "--storage", str(ws))
    assert_clean_failure(result, *fragments)
    shutil.rmtree(ws)


def test_a_storage_path_that_is_a_file_is_a_clean_error(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    occupied = tmp_path / "occupied"
    occupied.write_text("keep me", encoding="utf-8")
    assert_clean_failure(
        invoke(capsys, "documents", "list", "--storage", str(occupied))
    )
    assert occupied.read_text(encoding="utf-8") == "keep me"


def test_a_corrupt_catalog_file_is_reported_and_left_untouched(
    capsys: pytest.CaptureFixture[str], ws: Path
) -> None:
    ws.mkdir()
    damaged = b"this is not a database" * 50
    (ws / "catalog.sqlite3").write_bytes(damaged)
    result = invoke(capsys, "documents", "list", "--storage", str(ws))
    assert_clean_failure(result, "could not read the catalog", "corrupt")
    assert (ws / "catalog.sqlite3").read_bytes() == damaged
    shutil.rmtree(ws)


# --- the original commands share the same error handling -------------------------


def test_the_catalog_commands_report_errors_the_same_way(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = write_file(tmp_path, "notes.md")
    common = [
        "--catalog",
        str(tmp_path / "catalog.sqlite"),
        "--tenant",
        "tenant-a",
        "--source-id",
        "doc-1",
        "--source-type",
        "text/markdown",
        "--trusted-distribution",
        "dataindex-ext-documents",
        "--trusted-version",
        "0.1.0",
    ]
    result = invoke(capsys, "index", *common, "--file", str(path))
    assert_clean_failure(result, "--enable-plugin")

    missing = invoke(
        capsys,
        "index",
        *common,
        "--file",
        str(tmp_path / "absent.md"),
        "--enable-plugin",
    )
    assert_clean_failure(missing, "absent.md")
