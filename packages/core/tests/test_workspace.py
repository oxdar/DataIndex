from __future__ import annotations

import json
import sqlite3
import warnings
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing
from pathlib import Path

import pytest

from dataindex import local, local_runtime
from dataindex.exceptions import (
    ConfigurationError,
    DocumentNotFoundError,
    WorkspaceError,
)
from dataindex.models import JsonValue
from dataindex.sdk import DataIndexClient
from dataindex.workspace import (
    MAX_CONFIG_JSON_BYTES,
    MAX_DOCUMENT_NAME_CHARS,
    WORKSPACE_FILE_NAME,
    WORKSPACE_SCHEMA_VERSION,
    WorkspaceDocument,
    WorkspaceRegistry,
    validate_document_name,
)
from local_layer_fakes import (
    DOCUMENTS_DISTRIBUTION,
    FakeDocumentsPlugin,
    documents_entry_point,
)

DATABASE_CONFIG: dict[str, JsonValue] = {
    "allowed_columns": {"orders": ["id", "total"]},
    "queries": [
        {
            "query_id": "order-total",
            "table": "orders",
            "max_rows": 10,
            "ratio": 0.5,
            "enabled": True,
            "note": None,
            "label": "Résumé ✓",
        }
    ],
}
MALFORMED_ROW_MESSAGE = "the workspace registry contains a malformed document record"


def make_document(
    doc_id: str = "doc-1",
    *,
    name: str | None = None,
    kind: str = "document",
    version: str = "sha256:aaa",
    config: Mapping[str, JsonValue] | None = None,
) -> WorkspaceDocument:
    return WorkspaceDocument(
        doc_id=doc_id,
        name=name if name is not None else f"{doc_id}.md",
        kind=kind,
        source_type="application/vnd.sqlite3" if kind == "database" else "text/markdown",
        plugin_id="databases" if kind == "database" else "documents",
        path=f"C:\\data\\{doc_id}.md",
        version=version,
        config=config,
    )


def ids(documents: Sequence[WorkspaceDocument]) -> list[str]:
    return [document.doc_id for document in documents]


def stamp(path: Path, stamps: Mapping[str, str]) -> None:
    # submitted_at has millisecond resolution, so tests set it explicitly.
    with closing(sqlite3.connect(path)) as connection:
        for doc_id, submitted_at in stamps.items():
            connection.execute(
                "UPDATE documents SET submitted_at = ? WHERE doc_id = ?",
                (submitted_at, doc_id),
            )
        connection.commit()


def insert_raw(
    path: Path,
    *,
    doc_id: str = "doc-1",
    name: str = "note.md",
    kind: str = "document",
    version: str = "sha256:aaa",
    config_json: str | None = None,
) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "INSERT INTO documents(doc_id, name, kind, source_type, plugin_id, path, "
            "version, config_json) VALUES (?, ?, ?, 'text/markdown', 'documents', "
            "'C:\\data\\note.md', ?, ?)",
            (doc_id, name, kind, version, config_json),
        )
        connection.commit()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "workspace.sqlite3"


@pytest.fixture
def registry(db_path: Path) -> Iterator[WorkspaceRegistry]:
    with WorkspaceRegistry(db_path) as opened:
        yield opened


# ---------------------------------------------------------------------------
# Storing and finding documents


def test_upsert_and_get_round_trip(registry: WorkspaceRegistry) -> None:
    registry.upsert(make_document("doc-1", name="Quarterly plan.md", version="sha256:abc"))

    stored = registry.get("doc-1")

    assert stored is not None
    assert (stored.doc_id, stored.name, stored.kind) == ("doc-1", "Quarterly plan.md", "document")
    assert stored.version == "sha256:abc"
    assert stored.config is None
    assert stored.submitted_at is not None
    assert stored.submitted_at.startswith("20")
    assert registry.count() == 1


def test_database_configuration_round_trips_as_plain_json(
    registry: WorkspaceRegistry,
) -> None:
    registry.upsert(make_document("db-1", kind="database", config=DATABASE_CONFIG))

    stored = registry.get("db-1")

    assert stored is not None
    assert stored.config == DATABASE_CONFIG
    assert isinstance(stored.config, dict)
    query = stored.config["queries"][0]  # type: ignore[index]
    assert query["enabled"] is True
    assert query["note"] is None
    assert query["ratio"] == 0.5


def test_unknown_document_is_none_and_missing_delete_is_false(
    registry: WorkspaceRegistry,
) -> None:
    assert registry.get("doc-404") is None
    assert registry.get_by_name("nothing.md") is None
    assert registry.delete("doc-404") is False
    assert registry.count() == 0


def test_upsert_replaces_the_metadata_of_an_existing_document(
    registry: WorkspaceRegistry,
) -> None:
    registry.upsert(make_document("doc-1", name="old.md", version="sha256:old"))
    registry.upsert(make_document("doc-1", name="new.md", version="sha256:new"))

    stored = registry.get("doc-1")

    assert registry.count() == 1
    assert stored is not None
    assert (stored.name, stored.version) == ("new.md", "sha256:new")
    assert registry.get_by_name("old.md") is None


def test_upsert_can_drop_a_configuration(registry: WorkspaceRegistry) -> None:
    registry.upsert(make_document("db-1", kind="database", config=DATABASE_CONFIG))
    registry.upsert(make_document("db-1", kind="database", config=None))

    stored = registry.get("db-1")

    assert stored is not None
    assert stored.config is None


def test_delete_reports_presence_once(registry: WorkspaceRegistry) -> None:
    registry.upsert(make_document("doc-1"))
    registry.upsert(make_document("doc-2"))

    assert registry.delete("doc-1") is True
    assert registry.delete("doc-1") is False
    assert registry.get("doc-1") is None
    assert ids(registry.list_documents()) == ["doc-2"]
    assert registry.count() == 1


def test_get_by_name_returns_the_earliest_submitted_exact_match(
    registry: WorkspaceRegistry,
) -> None:
    registry.upsert(make_document("doc-b", name="same.md"))
    registry.upsert(make_document("doc-a", name="same.md"))
    registry.upsert(make_document("doc-c", name="Same.md"))

    holder = registry.get_by_name("same.md")
    assert holder is not None
    assert holder.doc_id == "doc-b"
    other = registry.get_by_name("Same.md")
    assert other is not None
    assert other.doc_id == "doc-c"
    assert registry.get_by_name("same.MD") is None

    registry.upsert(make_document("doc-a", name="same.md", version="sha256:new"))
    holder = registry.get_by_name("same.md")
    assert holder is not None
    assert holder.doc_id == "doc-b"

    registry.delete("doc-b")
    holder = registry.get_by_name("same.md")
    assert holder is not None
    assert holder.doc_id == "doc-a"


# ---------------------------------------------------------------------------
# Ordering and paging


def test_list_documents_defaults_to_submission_order(
    registry: WorkspaceRegistry,
) -> None:
    for doc_id in ("doc-c", "doc-a", "doc-b"):
        registry.upsert(make_document(doc_id))

    assert ids(registry.list_documents()) == ["doc-c", "doc-a", "doc-b"]


def test_newest_first_orders_by_submission_time(
    registry: WorkspaceRegistry, db_path: Path
) -> None:
    for doc_id in ("doc-a", "doc-b", "doc-c"):
        registry.upsert(make_document(doc_id))
    stamp(
        db_path,
        {
            "doc-a": "2024-01-01T00:00:00.000Z",
            "doc-b": "2024-03-01T00:00:00.000Z",
            "doc-c": "2024-02-01T00:00:00.000Z",
        },
    )

    assert ids(registry.list_documents(newest_first=True)) == ["doc-b", "doc-c", "doc-a"]
    assert ids(registry.list_documents()) == ["doc-a", "doc-b", "doc-c"]


def test_newest_first_breaks_equal_timestamps_by_insertion_order(
    registry: WorkspaceRegistry, db_path: Path
) -> None:
    for doc_id in ("doc-a", "doc-b", "doc-c"):
        registry.upsert(make_document(doc_id))
    stamp(db_path, {doc_id: "2024-01-01T00:00:00.000Z" for doc_id in ("doc-a", "doc-b", "doc-c")})

    assert ids(registry.list_documents(newest_first=True)) == ["doc-c", "doc-b", "doc-a"]


def test_resubmitting_moves_a_document_to_the_front_but_keeps_its_row_position(
    registry: WorkspaceRegistry, db_path: Path
) -> None:
    for doc_id in ("doc-a", "doc-b", "doc-c"):
        registry.upsert(make_document(doc_id))
    stamp(
        db_path,
        {
            "doc-a": "2024-01-01T00:00:00.000Z",
            "doc-b": "2024-01-02T00:00:00.000Z",
            "doc-c": "2024-01-03T00:00:00.000Z",
        },
    )

    registry.upsert(make_document("doc-a", name="renamed.md", version="sha256:new"))

    refreshed = registry.get("doc-a")
    assert refreshed is not None
    assert refreshed.submitted_at is not None
    assert refreshed.submitted_at > "2024-01-03T00:00:00.000Z"
    assert ids(registry.list_documents(newest_first=True)) == ["doc-a", "doc-c", "doc-b"]
    assert ids(registry.list_documents()) == ["doc-a", "doc-b", "doc-c"]


def test_list_documents_pages_with_limit_and_offset(
    registry: WorkspaceRegistry, db_path: Path
) -> None:
    names = [f"doc-{number}" for number in range(5)]
    for doc_id in names:
        registry.upsert(make_document(doc_id))
    stamp(db_path, {doc_id: f"2024-01-0{number + 1}T00:00:00.000Z" for number, doc_id in enumerate(names)})

    assert ids(registry.list_documents(limit=2)) == names[:2]
    assert ids(registry.list_documents(limit=2, offset=1)) == names[1:3]
    assert ids(registry.list_documents(limit=10, offset=4)) == names[4:]
    assert registry.list_documents(limit=2, offset=5) == ()
    assert ids(registry.list_documents(limit=2, offset=1, newest_first=True)) == [
        "doc-3",
        "doc-2",
    ]


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "5", None, 10_001])
def test_list_documents_rejects_invalid_limits(
    registry: WorkspaceRegistry, limit: object
) -> None:
    with pytest.raises(ValueError, match="limit must be between 1 and"):
        registry.list_documents(limit=limit)  # type: ignore[arg-type]


@pytest.mark.parametrize("offset", [-1, True, 1.5, "0", None])
def test_list_documents_rejects_invalid_offsets(
    registry: WorkspaceRegistry, offset: object
) -> None:
    with pytest.raises(ValueError, match="offset must be a non-negative integer"):
        registry.list_documents(offset=offset)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Bound parameters


@pytest.mark.parametrize(
    "hostile",
    ["x' OR '1'='1", "doc-1' --", "doc-1'; DROP TABLE documents; --", "%", "doc-1\x00", ""],
)
def test_hostile_identifiers_never_match_other_rows(
    registry: WorkspaceRegistry, hostile: str
) -> None:
    registry.upsert(make_document("doc-1", name="note.md"))

    assert registry.get(hostile) is None
    assert registry.get_by_name(hostile) is None
    assert registry.delete(hostile) is False
    assert registry.count() == 1
    assert registry.get("doc-1") is not None


def test_hostile_display_name_is_stored_verbatim(registry: WorkspaceRegistry) -> None:
    name = "a'; DROP TABLE documents; --"
    registry.upsert(make_document("doc-1", name=name))

    stored = registry.get_by_name(name)

    assert stored is not None
    assert stored.name == name
    assert registry.count() == 1


# ---------------------------------------------------------------------------
# Validation


@pytest.mark.parametrize(
    "doc_id",
    ["a", "A9", "doc-0123456789abcdef", "a.b_c:d-e", "a" * 128],
)
def test_ordinary_document_ids_are_accepted(doc_id: str) -> None:
    assert make_document(doc_id).doc_id == doc_id


@pytest.mark.parametrize(
    "doc_id",
    [
        "",
        "-lead",
        ".lead",
        "doc 1",
        "doc-1\n",
        "x' OR '1'='1",
        "../etc/passwd",
        "dok\u00e9",
        "a" * 129,
    ],
)
def test_unsafe_document_ids_are_rejected(doc_id: str) -> None:
    with pytest.raises(ValueError, match="doc_id must be"):
        make_document(doc_id)


def test_non_string_document_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="doc_id must be"):
        WorkspaceDocument(
            doc_id=7,  # type: ignore[arg-type]
            name="a.md",
            kind="document",
            source_type="text/markdown",
            plugin_id="documents",
            path="C:\\a.md",
            version="v",
        )


@pytest.mark.parametrize(
    "name",
    [
        "report.md",
        "x",
        "Q3 r\u00e9sum\u00e9 \u2713.txt",
        "it's \"quoted\".md",
        "a'; DROP TABLE documents; --",
        "x" * MAX_DOCUMENT_NAME_CHARS,
    ],
)
def test_ordinary_display_names_are_accepted(name: str) -> None:
    validate_document_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "\t",
        "x" * (MAX_DOCUMENT_NAME_CHARS + 1),
        "line\nbreak",
        "carriage\rreturn",
        "nul\x00byte",
        "tab\there",
        "del\x7fchar",
        "\x1b[31mred",
    ],
)
def test_unusable_display_names_are_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="name must"):
        validate_document_name(name)
    with pytest.raises(ValueError, match="name must"):
        make_document("doc-1", name=name)


@pytest.mark.parametrize("name", [None, 5, b"bytes", ["x"]])
def test_non_string_display_names_are_rejected(name: object) -> None:
    with pytest.raises(ValueError, match="name must"):
        validate_document_name(name)  # type: ignore[arg-type]


def test_document_kind_is_restricted() -> None:
    with pytest.raises(ValueError, match="kind must be"):
        make_document("doc-1", kind="spreadsheet")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_type", " ", "source_type must be"),
        ("source_type", "t" * 129, "source_type must be"),
        ("plugin_id", "", "plugin_id must be"),
        ("plugin_id", "p" * 129, "plugin_id must be"),
        ("path", "", "path must be"),
        ("path", "p" * 4097, "path must be"),
        ("version", "  ", "version must be"),
        ("version", "v" * 513, "version must be"),
    ],
)
def test_metadata_fields_are_bounded(field: str, value: str, message: str) -> None:
    fields = {
        "doc_id": "doc-1",
        "name": "a.md",
        "kind": "document",
        "source_type": "text/markdown",
        "plugin_id": "documents",
        "path": "C:\\a.md",
        "version": "sha256:aaa",
    }
    fields[field] = value

    with pytest.raises(ValueError, match=message):
        WorkspaceDocument(**fields)


def test_only_databases_carry_a_configuration() -> None:
    with pytest.raises(ValueError, match="only databases carry a configuration"):
        make_document("doc-1", kind="document", config={"queries": []})


def test_configuration_must_be_a_mapping() -> None:
    with pytest.raises(ValueError, match="config must be a mapping"):
        make_document("db-1", kind="database", config=["queries"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Configuration encoding


def nested(levels: int) -> dict[str, JsonValue]:
    value: JsonValue = 1
    for _ in range(levels):
        value = {"a": value}
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize(
    "config",
    [
        {"ratio": float("nan")},
        {"ratio": float("inf")},
        {"unordered": {1, 2}},
        {"raw": b"bytes"},
        {"object": object()},
        nested(17),
    ],
    ids=["nan", "infinity", "set", "bytes", "object", "too-deep"],
)
def test_configuration_that_is_not_plain_json_writes_nothing(
    registry: WorkspaceRegistry, config: Mapping[str, JsonValue]
) -> None:
    registry.upsert(make_document("db-1", kind="database", config=DATABASE_CONFIG))

    with pytest.raises(ValueError, match="not plain JSON"):
        registry.upsert(
            make_document("db-1", kind="database", name="changed.md", config=config)
        )
    with pytest.raises(ValueError, match="not plain JSON"):
        registry.upsert(make_document("db-2", kind="database", config=config))

    stored = registry.get("db-1")
    assert stored is not None
    assert stored.name == "db-1.md"
    assert stored.config == DATABASE_CONFIG
    assert registry.get("db-2") is None


def test_configuration_nesting_boundary(registry: WorkspaceRegistry) -> None:
    registry.upsert(make_document("db-1", kind="database", config=nested(16)))

    stored = registry.get("db-1")
    assert stored is not None
    assert stored.config == nested(16)


def test_configuration_size_limit_boundary(registry: WorkspaceRegistry) -> None:
    def config_of(size: int) -> dict[str, JsonValue]:
        config: dict[str, JsonValue] = {"k": "a" * (size - len('{"k":""}'))}
        assert len(json.dumps(config, separators=(",", ":")).encode("utf-8")) == size
        return config

    registry.upsert(
        make_document("db-1", kind="database", config=config_of(MAX_CONFIG_JSON_BYTES))
    )
    with pytest.raises(ValueError, match="exceeds"):
        registry.upsert(
            make_document(
                "db-2", kind="database", config=config_of(MAX_CONFIG_JSON_BYTES + 1)
            )
        )

    assert registry.get("db-1") is not None
    assert registry.get("db-2") is None


# ---------------------------------------------------------------------------
# Persistence and unusable files


def test_documents_persist_across_reopen(db_path: Path) -> None:
    with WorkspaceRegistry(db_path) as first:
        first.upsert(make_document("doc-1", name="one.md"))
        first.upsert(make_document("db-1", kind="database", config=DATABASE_CONFIG))
        assert first.schema_version == WORKSPACE_SCHEMA_VERSION

    with WorkspaceRegistry(db_path) as second:
        assert second.schema_version == WORKSPACE_SCHEMA_VERSION
        assert second.count() == 2
        assert ids(second.list_documents()) == ["doc-1", "db-1"]
        database = second.get("db-1")
        assert database is not None
        assert database.config == DATABASE_CONFIG
        second.delete("doc-1")

    with WorkspaceRegistry(db_path) as third:
        assert ids(third.list_documents()) == ["db-1"]


def test_garbage_file_is_reported_without_touching_or_locking_it(db_path: Path) -> None:
    content = b"definitely not a sqlite database " * 64
    db_path.write_bytes(content)

    with pytest.raises(WorkspaceError, match="corrupt or not a DataIndex workspace") as error:
        WorkspaceRegistry(db_path)

    assert str(db_path) not in str(error.value)
    assert db_path.read_bytes() == content
    db_path.unlink()
    assert not db_path.exists()


def test_newer_schema_is_refused_and_left_untouched(db_path: Path) -> None:
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(f"PRAGMA user_version = {WORKSPACE_SCHEMA_VERSION + 1}")

    with pytest.raises(WorkspaceError, match="newer than the supported schema"):
        WorkspaceRegistry(db_path)

    with closing(sqlite3.connect(db_path)) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = connection.execute("SELECT name FROM sqlite_master").fetchall()
    assert version == WORKSPACE_SCHEMA_VERSION + 1
    assert tables == []
    db_path.unlink()


def test_foreign_database_is_not_overwritten(db_path: Path) -> None:
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("CREATE TABLE documents (title TEXT)")
        connection.execute("INSERT INTO documents VALUES ('keep me')")
        connection.commit()

    with pytest.raises(WorkspaceError, match="the workspace registry failed"):
        WorkspaceRegistry(db_path)

    with closing(sqlite3.connect(db_path)) as connection:
        assert connection.execute("SELECT title FROM documents").fetchall() == [("keep me",)]
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    db_path.unlink()


def test_unopenable_location_is_a_workspace_error_that_hides_the_path(
    tmp_path: Path,
) -> None:
    for location in (tmp_path, tmp_path / "missing-parent" / "workspace.sqlite3"):
        with pytest.raises(WorkspaceError, match="could not open the workspace registry") as error:
            WorkspaceRegistry(location)
        assert str(tmp_path) not in str(error.value)


@pytest.mark.parametrize(
    "row",
    [
        {"doc_id": "bad id"},
        {"name": "line\nbreak"},
        {"name": "   "},
        {"kind": "database", "config_json": "{not json"},
        {"kind": "database", "config_json": "[1, 2]"},
        {"kind": "document", "config_json": '{"a": 1}'},
    ],
    ids=[
        "bad-doc-id",
        "control-character-name",
        "blank-name",
        "config-not-json",
        "config-not-an-object",
        "config-on-a-document",
    ],
)
def test_malformed_rows_fail_with_a_fixed_message(
    registry: WorkspaceRegistry, db_path: Path, row: dict[str, str]
) -> None:
    secret = "SECRET-ROW-CONTENT"
    doc_id = row.get("doc_id", "doc-1")
    insert_raw(db_path, version=secret, **row)

    for operation in (lambda: registry.get(doc_id), registry.list_documents):
        with pytest.raises(WorkspaceError) as error:
            operation()
        assert str(error.value) == MALFORMED_ROW_MESSAGE

    # The bad row can still be removed without being decoded.
    assert registry.delete(doc_id) is True
    assert registry.list_documents() == ()


def test_closed_registry_refuses_every_operation(db_path: Path) -> None:
    registry = WorkspaceRegistry(db_path)
    registry.upsert(make_document("doc-1"))
    registry.close()
    registry.close()

    operations = (
        lambda: registry.get("doc-1"),
        lambda: registry.get_by_name("doc-1.md"),
        registry.count,
        registry.list_documents,
        lambda: registry.delete("doc-1"),
        lambda: registry.upsert(make_document("doc-2")),
        lambda: registry.schema_version,
    )
    for operation in operations:
        with pytest.raises(WorkspaceError, match="closed"):
            operation()
    with pytest.raises(WorkspaceError, match="closed"), registry:
        pass


def test_context_manager_closes_the_registry(db_path: Path) -> None:
    with WorkspaceRegistry(db_path) as opened:
        opened.upsert(make_document("doc-1"))

    with pytest.raises(WorkspaceError, match="closed"):
        opened.count()


# ---------------------------------------------------------------------------
# Name and capacity rules enforced by the local client on top of the registry


@pytest.fixture
def plugin(monkeypatch: pytest.MonkeyPatch) -> FakeDocumentsPlugin:
    fake = FakeDocumentsPlugin()
    entry_point = documents_entry_point(fake)
    monkeypatch.setattr(local, "entry_points", lambda *, group: (entry_point,))
    return fake


@pytest.fixture
def client(tmp_path: Path, plugin: FakeDocumentsPlugin) -> Iterator[DataIndexClient]:
    with DataIndexClient(
        storage_path=tmp_path / "store",
        trusted_plugins=[(DOCUMENTS_DISTRIBUTION, "0.1.0")],
    ) as opened:
        yield opened


def write_note(directory: Path, name: str = "notes.md", text: str = "# Title\n\nBody\n") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def test_second_file_with_the_same_name_gets_a_numeric_suffix(
    client: DataIndexClient, tmp_path: Path
) -> None:
    first = client.submit_document(write_note(tmp_path / "one"))
    with pytest.warns(UserWarning, match="'notes.md' already exists.*'notes_1.md'"):
        second = client.submit_document(write_note(tmp_path / "two"))
    with pytest.warns(UserWarning, match="notes_2.md"):
        third = client.submit_document(write_note(tmp_path / "three"))

    assert [first["name"], second["name"], third["name"]] == [
        "notes.md",
        "notes_1.md",
        "notes_2.md",
    ]
    assert len({first["doc_id"], second["doc_id"], third["doc_id"]}) == 3
    assert client.get_document_id("notes.md") == first["doc_id"]
    assert client.get_document_id("notes_1.md") == second["doc_id"]
    assert client.get_document_id("notes_2.md") == third["doc_id"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("report.md", "report_1.md"),
        ("readme", "readme_1"),
        ("v1.2.notes.md", "v1.2.notes_1.md"),
        ("notes." + "e" * 40, "notes." + "e" * 40 + "_1"),
    ],
    ids=["extension", "no-extension", "dots", "long-extension"],
)
def test_suffix_goes_before_a_short_extension_for_explicit_names(
    client: DataIndexClient, tmp_path: Path, name: str, expected: str
) -> None:
    first = client.submit_document(write_note(tmp_path / "one"), name=name)
    with pytest.warns(UserWarning):
        second = client.submit_document(write_note(tmp_path / "two"), name=name)

    assert first["name"] == name
    assert second["name"] == expected


def test_suffixed_name_is_shortened_to_stay_within_the_length_limit(
    client: DataIndexClient, tmp_path: Path
) -> None:
    long_name = "n" * (MAX_DOCUMENT_NAME_CHARS - len(".txt")) + ".txt"
    assert len(long_name) == MAX_DOCUMENT_NAME_CHARS

    client.submit_document(write_note(tmp_path / "one"), name=long_name)
    with pytest.warns(UserWarning):
        second = client.submit_document(write_note(tmp_path / "two"), name=long_name)

    assert second["name"] == "n" * (MAX_DOCUMENT_NAME_CHARS - len("_1.txt")) + "_1.txt"
    assert len(str(second["name"])) == MAX_DOCUMENT_NAME_CHARS
    validate_document_name(str(second["name"]))


def test_resubmitting_a_file_keeps_its_suffixed_name_without_a_warning(
    client: DataIndexClient, tmp_path: Path
) -> None:
    client.submit_document(write_note(tmp_path / "one"))
    second_path = write_note(tmp_path / "two")
    with pytest.warns(UserWarning):
        client.submit_document(second_path)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        again = client.submit_document(second_path)

    assert again["name"] == "notes_1.md"
    assert again["change"] == "unchanged"


def test_deleting_a_document_frees_its_name(client: DataIndexClient, tmp_path: Path) -> None:
    first = client.submit_document(write_note(tmp_path / "one"))
    assert client.delete_document(str(first["doc_id"])) is True

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        second = client.submit_document(write_note(tmp_path / "two"))

    assert second["name"] == "notes.md"


def seed_names(store: Path, names: Sequence[str]) -> None:
    # Direct inserts keep tests that need many rows fast; every commit is an fsync.
    with closing(sqlite3.connect(store / WORKSPACE_FILE_NAME)) as connection:
        connection.executemany(
            "INSERT INTO documents(doc_id, name, kind, source_type, plugin_id, path, "
            "version) VALUES (?, ?, 'document', 'text/markdown', 'documents', "
            "'C:\\seed.md', 'sha256:seed')",
            [(f"seed-{number}", name) for number, name in enumerate(names)],
        )
        connection.commit()


@pytest.mark.filterwarnings("ignore::UserWarning")
def test_numeric_suffixes_stop_at_99_and_nothing_is_registered_afterwards(
    client: DataIndexClient, plugin: FakeDocumentsPlugin, tmp_path: Path
) -> None:
    taken = ["notes.md"] + [f"notes_{number}.md" for number in range(1, 99)]
    seed_names(tmp_path / "store", taken)

    last = client.submit_document(write_note(tmp_path / "one"))
    with pytest.raises(
        ConfigurationError, match=r"99 documents already use names like 'notes\.md'"
    ):
        client.submit_document(write_note(tmp_path / "two"))

    assert last["name"] == "notes_99.md"
    assert client.list_documents(limit=1)["total"] == 100
    assert len(plugin.registered) == 1
    # An explicit, different name is still accepted.
    other = client.submit_document(write_note(tmp_path / "two"), name="elsewhere.md")
    assert other["name"] == "elsewhere.md"


def test_workspace_cap_refuses_new_documents_but_not_resubmission(
    client: DataIndexClient,
    plugin: FakeDocumentsPlugin,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(local_runtime, "MAX_WORKSPACE_DOCUMENTS", 2)
    first_path = write_note(tmp_path / "one", "a.md")
    first = client.submit_document(first_path)
    client.submit_document(write_note(tmp_path / "two", "b.md"))

    with pytest.raises(ConfigurationError, match="already holds 2 documents"):
        client.submit_document(write_note(tmp_path / "three", "c.md"))

    assert client.list_documents()["total"] == 2
    assert len(plugin.registered) == 2
    assert client.submit_document(first_path)["change"] == "unchanged"
    assert client.delete_document(str(first["doc_id"])) is True
    assert client.submit_document(write_note(tmp_path / "three", "c.md"))["change"] == "new"


def test_workspace_file_never_contains_submitted_content(
    client: DataIndexClient, tmp_path: Path
) -> None:
    secret = "CONFIDENTIAL-MARKER-7f3a9c"
    client.submit_document(write_note(tmp_path / "one", text=f"# Plan\n\n{secret}\n"))

    stored = (tmp_path / "store" / WORKSPACE_FILE_NAME).read_bytes()

    assert b"notes.md" in stored
    assert secret.encode() not in stored


def test_unknown_document_id_error_echoes_a_bounded_prefix(
    client: DataIndexClient, tmp_path: Path
) -> None:
    client.submit_document(write_note(tmp_path / "one"))

    with pytest.raises(DocumentNotFoundError):
        client.get_document("x' OR '1'='1")
    with pytest.raises(DocumentNotFoundError) as error:
        client.get_document("a" * 500)

    assert "a" * 65 not in str(error.value)
    assert client.list_documents()["total"] == 1
