"""Local workspace commands for the ``dataindex`` command line.

``submit``, ``submit-db``, ``documents`` and ``chat`` drive the same
``DataIndexClient`` workflow as the Python API: one storage directory that holds
the catalog and the workspace, only the installed first-party plugins, and only
the files the user submitted. Text that comes from documents or from a chat
model is shown without control characters so it cannot rewrite the terminal;
``--json`` output is escaped by JSON itself.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import warnings
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path

from dataindex.exceptions import (
    ConfigurationError,
    DataIndexError,
    DocumentNotFoundError,
)
from dataindex.local import DATABASE_SUFFIXES, DEFAULT_STORAGE_PATH
from dataindex.models import JsonValue
from dataindex.sdk import DataIndexClient
from dataindex.workspace import MAX_CONFIG_JSON_BYTES

LOCAL_COMMANDS = frozenset({"submit", "submit-db", "documents", "chat"})
COMMAND_ERRORS = (DataIndexError, OSError, TimeoutError, ValueError)
MODEL_ENVIRONMENT_VARIABLE = "DATAINDEX_MODEL"
_QUIT_WORDS = frozenset({"exit", "quit"})
_UNSAFE_TEXT = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def printable(text: str) -> str:
    """Replace control and bidirectional-override characters with ``?``."""
    return _UNSAFE_TEXT.sub("?", text.replace("\r\n", "\n"))


def add_local_commands(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    submit = commands.add_parser(
        "submit", help="index a Markdown, text or HTML file in the workspace"
    )
    submit.add_argument("path", type=Path)
    submit.add_argument("--name", help="display name (default: the file name)")
    _add_storage(submit)
    _add_json(submit)

    submit_db = commands.add_parser(
        "submit-db",
        help="register a SQLite database with an approved set of read-only queries",
    )
    submit_db.add_argument("path", type=Path)
    submit_db.add_argument(
        "--config",
        required=True,
        type=Path,
        metavar="FILE",
        help="JSON file with the allowed_columns and queries objects",
    )
    submit_db.add_argument("--name", help="display name (default: the file name)")
    _add_storage(submit_db)
    _add_json(submit_db)

    documents = commands.add_parser(
        "documents", help="list, inspect or remove submitted documents"
    )
    actions = documents.add_subparsers(dest="documents_command", required=True)
    listing = actions.add_parser("list", help="list submitted documents, newest first")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--offset", type=int, default=0)
    _add_storage(listing)
    _add_json(listing)
    show = actions.add_parser("get", help="show one document as JSON")
    _add_reference(show)
    _add_storage(show)
    tree = actions.add_parser("tree", help="show a document's section outline")
    _add_reference(tree)
    tree.add_argument("--max-entries", type=int, metavar="N")
    _add_storage(tree)
    _add_json(tree)
    section = actions.add_parser("section", help="show one section's text")
    _add_reference(section)
    section.add_argument("node_id", metavar="NODE_ID")
    _add_storage(section)
    _add_json(section)
    delete = actions.add_parser(
        "delete", help="remove a document from the workspace (the file is untouched)"
    )
    _add_reference(delete)
    _add_storage(delete)

    chat = commands.add_parser(
        "chat",
        help="ask a question about the submitted documents",
        description=(
            "Ask a question and print a cited answer. Without QUESTION, read "
            "questions from standard input until exit or end of input. Every "
            "question is answered on its own. The configured model provider "
            "receives the question, document outlines and bounded excerpts."
        ),
    )
    chat.add_argument("question", nargs="?", metavar="QUESTION")
    chat.add_argument(
        "--doc",
        action="append",
        dest="references",
        metavar="DOC",
        help="document ID or name to read; repeat for several (default: all)",
    )
    chat.add_argument(
        "--model",
        metavar="NAME",
        help=f"chat model name (default: ${MODEL_ENVIRONMENT_VARIABLE})",
    )
    chat.add_argument(
        "--instructions",
        metavar="TEXT",
        help="extra guidance for the model, at most 4000 characters",
    )
    _add_storage(chat)
    _add_json(chat)


def run_local_command(args: argparse.Namespace) -> int:
    _replace_unencodable_output()
    with _reported_warnings():
        if args.command in {"submit", "submit-db"}:
            return _submit(args)
        if args.command == "documents":
            return _documents(args)
        return _chat(args)


def _add_storage(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--storage",
        default=DEFAULT_STORAGE_PATH,
        metavar="DIR",
        help=f"workspace directory, created if missing (default: {DEFAULT_STORAGE_PATH})",
    )


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print JSON")


def _add_reference(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "document",
        metavar="DOC",
        help="document ID or exact name; an ID wins over a name",
    )


def _replace_unencodable_output() -> None:
    """Show ``?`` instead of failing on characters the console cannot encode."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with suppress(OSError, ValueError):
                reconfigure(errors="replace")


@contextmanager
def _reported_warnings() -> Generator[None, None, None]:
    """Print library warnings, such as a renamed duplicate, as CLI messages."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        try:
            yield
        finally:
            for item in caught:
                _print_warning(str(item.message))


def _print_warning(message: str) -> None:
    print(f"dataindex: warning: {printable(message)}", file=sys.stderr)


# ----------------------------------------------------------------------
# submit and submit-db


def _submit(args: argparse.Namespace) -> int:
    path: Path = args.path
    is_database = args.command == "submit-db"
    is_database_file = path.suffix.lower() in DATABASE_SUFFIXES
    if is_database and not is_database_file:
        raise ConfigurationError(
            f"{path.name!r} is not a SQLite database; submit documents with: "
            "dataindex submit PATH"
        )
    if is_database_file and not is_database:
        raise ConfigurationError(
            f"{path.name!r} is a SQLite database; submit it with: "
            "dataindex submit-db PATH --config FILE"
        )
    config = _read_database_config(args.config) if is_database else None
    with DataIndexClient(storage_path=args.storage) as client:
        if config is None:
            result = client.submit_document(path, name=args.name)
        else:
            result = client.submit_database(
                path,
                allowed_columns=config[0],
                queries=config[1],
                name=args.name,
            )
    if args.json:
        _print_json(result)
    else:
        print(
            f"Submitted {_field(result, 'name')} as {_field(result, 'doc_id')} "
            f"({_field(result, 'change')}, {_field(result, 'node_count')} nodes)"
        )
    return 0


def _read_database_config(path: Path) -> tuple[dict[str, object], list[object]]:
    with path.open("rb") as handle:
        raw = handle.read(MAX_CONFIG_JSON_BYTES + 1)
    if len(raw) > MAX_CONFIG_JSON_BYTES:
        raise ConfigurationError(
            f"{path.name!r} is larger than {MAX_CONFIG_JSON_BYTES // 1024} KiB"
        )
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as error:
        raise ConfigurationError(f"{path.name!r} is not valid JSON: {error}") from None
    if (
        not isinstance(data, dict)
        or set(data) != {"allowed_columns", "queries"}
        or not isinstance(data["allowed_columns"], dict)
        or not isinstance(data["queries"], list)
    ):
        raise ConfigurationError(
            f"{path.name!r} must be a JSON object with an 'allowed_columns' object "
            "and a 'queries' list, and no other keys"
        )
    return data["allowed_columns"], data["queries"]


# ----------------------------------------------------------------------
# documents


def _documents(args: argparse.Namespace) -> int:
    actions: dict[str, Callable[[DataIndexClient, argparse.Namespace], None]] = {
        "list": _list_documents,
        "get": _get_document,
        "tree": _show_tree,
        "section": _show_section,
        "delete": _delete_document,
    }
    with DataIndexClient(storage_path=args.storage) as client:
        actions[args.documents_command](client, args)
    return 0


def _find_document(client: DataIndexClient, reference: str) -> dict[str, JsonValue]:
    """Look up a document by ID, then by exact name."""
    with suppress(DocumentNotFoundError):
        return client.get_document(reference)
    try:
        doc_id = client.get_document_id(reference)
    except DocumentNotFoundError:
        raise DocumentNotFoundError(
            f"no document has the ID or name {reference[:64]!r}"
        ) from None
    return client.get_document(doc_id)


def _list_documents(client: DataIndexClient, args: argparse.Namespace) -> None:
    result = client.list_documents(limit=args.limit, offset=args.offset)
    if args.json:
        _print_json(result)
        return
    documents = result["documents"]
    rows = (
        [item for item in documents if isinstance(item, dict)]
        if isinstance(documents, list)
        else []
    )
    total = _integer(result["total"])
    offset = _integer(result["offset"])
    if not rows:
        print(
            "No documents have been submitted. Add one with: dataindex submit PATH"
            if total == 0
            else f"No documents at offset {offset}; the workspace holds {total}."
        )
        return
    lines = _table(
        ("ID", "KIND", "STATUS", "NAME"),
        [
            (
                _field(row, "doc_id"),
                _field(row, "kind"),
                _field(row, "status"),
                _field(row, "name"),
            )
            for row in rows
        ],
    )
    lines.extend(
        f"! {_field(row, 'name')}: {_field(row, 'detail')}"
        for row in rows
        if "detail" in row
    )
    shown = offset + len(rows)
    if shown < total:
        lines.append(
            f"Showing {offset + 1}-{shown} of {total}; use --offset {shown} for more."
        )
    print("\n".join(lines))


def _get_document(client: DataIndexClient, args: argparse.Namespace) -> None:
    _print_json(_find_document(client, args.document))


def _show_tree(client: DataIndexClient, args: argparse.Namespace) -> None:
    info = _find_document(client, args.document)
    result = client.get_tree(str(info["doc_id"]), max_entries=args.max_entries)
    if args.json:
        _print_json(result)
        return
    lines = [f"{_field(result, 'name')} ({_field(result, 'doc_id')})"]
    roots = result["result"]
    stack: list[tuple[JsonValue, int]] = (
        [(node, 1) for node in reversed(roots)] if isinstance(roots, list) else []
    )
    while stack:
        node, depth = stack.pop()
        if not isinstance(node, dict):
            continue
        line = f"{'  ' * depth}{_field(node, 'title')}  [{_field(node, 'node_id')}]"
        span = _line_span(node.get("locator"))
        if span is not None:
            line += f"  {span}"
        lines.append(line)
        children = node.get("nodes")
        if isinstance(children, list):
            stack.extend((child, depth + 1) for child in reversed(children))
    if result["truncated"] is True:
        lines.append("(outline truncated)")
    print("\n".join(lines))


def _show_section(client: DataIndexClient, args: argparse.Namespace) -> None:
    info = _find_document(client, args.document)
    result = client.get_section(str(info["doc_id"]), args.node_id)
    if args.json:
        _print_json(result)
        return
    span = _line_span(result["locator"])
    heading = f"{_field(result, 'title')} - {_field(result, 'name')}"
    if span is not None:
        heading += f", {span}"
    print(f"{heading}\n\n{_field(result, 'text')}")


def _delete_document(client: DataIndexClient, args: argparse.Namespace) -> None:
    info = _find_document(client, args.document)
    client.delete_document(str(info["doc_id"]))
    print(
        f"Deleted {_field(info, 'name')} ({_field(info, 'doc_id')}) from the "
        "workspace; the file itself was not changed."
    )


# ----------------------------------------------------------------------
# chat


def _chat(args: argparse.Namespace) -> int:
    model = _model_name(args.model)
    with DataIndexClient(
        model, storage_path=args.storage, instructions=args.instructions
    ) as client:
        doc_ids = (
            [str(_find_document(client, item)["doc_id"]) for item in args.references]
            if args.references
            else None
        )
        if args.question is not None:
            _answer(client, args.question, doc_ids, args.json)
            return 0
        return _converse(client, doc_ids, args.json)


def _model_name(option: str | None) -> str:
    name = (option or os.environ.get(MODEL_ENVIRONMENT_VARIABLE, "")).strip()
    if not name:
        raise ConfigurationError(
            f"no chat model; pass --model NAME or set {MODEL_ENVIRONMENT_VARIABLE}"
        )
    return name


def _answer(
    client: DataIndexClient,
    question: str,
    doc_ids: Sequence[str] | None,
    as_json: bool,
) -> None:
    answer = client.chat(question, doc_id=doc_ids)
    if as_json:
        _print_json(answer.to_dict())
        return
    print(printable(str(answer)))
    for message in answer.warnings:
        _print_warning(message)


def _converse(
    client: DataIndexClient, doc_ids: Sequence[str] | None, as_json: bool
) -> int:
    interactive = sys.stdin.isatty()
    if interactive:
        print("Ask about your documents. Type exit to quit.", file=sys.stderr)
    failed = False
    for question in _questions(interactive):
        try:
            _answer(client, question, doc_ids, as_json)
        except COMMAND_ERRORS as error:
            print(f"dataindex: {printable(str(error))}", file=sys.stderr)
            failed = True
        if interactive and not as_json:
            print()
    return 2 if failed else 0


def _questions(prompt: bool) -> Iterator[str]:
    while True:
        if prompt:
            try:
                line = input("> ")
            except EOFError:
                print()
                return
        else:
            line = sys.stdin.readline()
            if not line:
                return
        text = line.strip()
        if text.lower() in _QUIT_WORDS:
            return
        if text:
            yield text


# ----------------------------------------------------------------------
# Output helpers


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2))


def _field(mapping: Mapping[str, JsonValue], key: str) -> str:
    return printable(str(mapping.get(key, "")))


def _integer(value: JsonValue) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _line_span(locator: JsonValue) -> str | None:
    if not isinstance(locator, dict):
        return None
    value = locator.get("value")
    if not isinstance(value, dict):
        return None
    start = value.get("line_start")
    end = value.get("line_end")
    if all(isinstance(item, int) and not isinstance(item, bool) for item in (start, end)):
        return f"lines {start}-{end}"
    return None


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    widths = [max(len(cell) for cell in column) for column in zip(headers, *rows)]
    return [
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip()
        for row in (headers, *rows)
    ]


__all__ = [
    "COMMAND_ERRORS",
    "LOCAL_COMMANDS",
    "MODEL_ENVIRONMENT_VARIABLE",
    "add_local_commands",
    "printable",
    "run_local_command",
]
