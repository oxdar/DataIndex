"""Local workspace registry for files submitted through ``DataIndexClient``.

The registry stores only what is needed to find a submitted file again: a stable
document ID, display name, source type, owning plugin, absolute path, the
source version recorded at submit time and, for databases, the typed
read-only query configuration. It never stores file content, so the native
source stays authoritative and nothing here can leak source data.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from dataindex._json import plain_json
from dataindex.exceptions import WorkspaceError
from dataindex.models import JsonValue

WORKSPACE_SCHEMA_VERSION = 1
WORKSPACE_FILE_NAME = "workspace.sqlite3"
MAX_WORKSPACE_DOCUMENTS = 10_000
MAX_DOCUMENT_NAME_CHARS = 256
MAX_DOCUMENT_PATH_CHARS = 4_096
MAX_CONFIG_JSON_BYTES = 256 * 1024
DOCUMENT_KINDS = frozenset({"document", "database"})

_DOC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SCHEMA_STATEMENTS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE documents (
            doc_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('document', 'database')),
            source_type TEXT NOT NULL,
            plugin_id TEXT NOT NULL,
            path TEXT NOT NULL,
            version TEXT NOT NULL,
            submitted_at TEXT NOT NULL DEFAULT (
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            ),
            config_json TEXT
        )
        """,
    ),
}


@dataclass(frozen=True, slots=True)
class WorkspaceDocument:
    """One submitted file, identified by its stable ``doc_id``."""

    doc_id: str
    name: str
    kind: str
    source_type: str
    plugin_id: str
    path: str
    version: str
    submitted_at: str | None = None
    config: Mapping[str, JsonValue] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.doc_id, str) or _DOC_ID.fullmatch(self.doc_id) is None:
            raise ValueError("doc_id must be 1-128 letters, digits or . _ : -")
        validate_document_name(self.name)
        if self.kind not in DOCUMENT_KINDS:
            raise ValueError("kind must be 'document' or 'database'")
        _require_text("source_type", self.source_type, 128)
        _require_text("plugin_id", self.plugin_id, 128)
        _require_text("path", self.path, MAX_DOCUMENT_PATH_CHARS)
        _require_text("version", self.version, 512)
        if self.config is not None:
            if self.kind != "database":
                raise ValueError("only databases carry a configuration")
            if not isinstance(self.config, Mapping):
                raise ValueError("config must be a mapping or None")


def validate_document_name(name: str) -> None:
    """Raise ``ValueError`` unless ``name`` is a usable display name."""
    _require_text("name", name, MAX_DOCUMENT_NAME_CHARS)
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise ValueError("name must not contain control characters")


class WorkspaceRegistry:
    """A versioned SQLite table of submitted documents.

    Writes use ``BEGIN IMMEDIATE`` like the catalog. The registry is not
    thread-safe; open one instance per thread or process.
    """

    def __init__(self, path: str | Path) -> None:
        try:
            self._connection = sqlite3.connect(str(path), isolation_level=None)
        except sqlite3.Error as error:
            raise WorkspaceError(
                f"could not open the workspace registry ({type(error).__name__})"
            ) from error
        self._closed = False
        try:
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._migrate()
        except sqlite3.Error as error:
            self.close()
            raise WorkspaceError(
                f"could not read the workspace registry ({type(error).__name__}); "
                "the file may be corrupt or not a DataIndex workspace"
            ) from error
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> WorkspaceRegistry:
        self._ensure_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._connection.close()

    @property
    def schema_version(self) -> int:
        return self._user_version()

    def upsert(self, document: WorkspaceDocument) -> None:
        """Insert a document or replace its metadata, keeping its row position."""
        config_json = _encode_config(document.config)
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO documents(doc_id, name, kind, source_type, plugin_id, "
                "path, version, config_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(doc_id) DO UPDATE SET name = excluded.name, "
                "kind = excluded.kind, source_type = excluded.source_type, "
                "plugin_id = excluded.plugin_id, path = excluded.path, "
                "version = excluded.version, config_json = excluded.config_json, "
                "submitted_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
                (
                    document.doc_id,
                    document.name,
                    document.kind,
                    document.source_type,
                    document.plugin_id,
                    document.path,
                    document.version,
                    config_json,
                ),
            )

    def get(self, doc_id: str) -> WorkspaceDocument | None:
        connection = self._open_connection()
        with _database_errors():
            row = connection.execute(
                "SELECT * FROM documents WHERE doc_id = ?", (doc_id,)
            ).fetchone()
        return None if row is None else _decode(row)

    def get_by_name(self, name: str) -> WorkspaceDocument | None:
        """Return the earliest submitted document with this exact display name."""
        connection = self._open_connection()
        with _database_errors():
            row = connection.execute(
                "SELECT * FROM documents WHERE name = ? ORDER BY rowid LIMIT 1",
                (name,),
            ).fetchone()
        return None if row is None else _decode(row)

    def count(self) -> int:
        connection = self._open_connection()
        with _database_errors():
            return int(
                connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            )

    def list_documents(
        self,
        *,
        limit: int = MAX_WORKSPACE_DOCUMENTS,
        offset: int = 0,
        newest_first: bool = False,
    ) -> tuple[WorkspaceDocument, ...]:
        """List documents in submission order, or most recently submitted first."""
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_WORKSPACE_DOCUMENTS
        ):
            raise ValueError(f"limit must be between 1 and {MAX_WORKSPACE_DOCUMENTS}")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        order = "submitted_at DESC, rowid DESC" if newest_first else "rowid"
        connection = self._open_connection()
        with _database_errors():
            rows = connection.execute(
                f"SELECT * FROM documents ORDER BY {order} LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return tuple(_decode(row) for row in rows)

    def delete(self, doc_id: str) -> bool:
        """Remove a document; return whether it was present."""
        with self._transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM documents WHERE doc_id = ?", (doc_id,)
            )
        return cursor.rowcount > 0

    def _ensure_open(self) -> None:
        if self._closed:
            raise WorkspaceError("the workspace registry is closed")

    def _open_connection(self) -> sqlite3.Connection:
        self._ensure_open()
        return self._connection

    def _user_version(self) -> int:
        connection = self._open_connection()
        with _database_errors():
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection, None, None]:
        connection = self._open_connection()
        with _database_errors():
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _migrate(self) -> None:
        connection = self._open_connection()
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        _check_supported(version)
        if version == WORKSPACE_SCHEMA_VERSION:
            return
        with self._transaction():
            # Re-read under the write lock: another process may have migrated.
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            _check_supported(version)
            for target in range(version + 1, WORKSPACE_SCHEMA_VERSION + 1):
                for statement in _SCHEMA_STATEMENTS[target]:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {target}")


def _check_supported(version: int) -> None:
    if version > WORKSPACE_SCHEMA_VERSION:
        raise WorkspaceError(
            f"the workspace schema {version} is newer than the supported schema "
            f"{WORKSPACE_SCHEMA_VERSION}; upgrade dataindex-core"
        )


@contextmanager
def _database_errors() -> Generator[None, None, None]:
    try:
        yield
    except sqlite3.Error as error:
        raise WorkspaceError(
            f"the workspace registry failed ({type(error).__name__})"
        ) from error


def _encode_config(config: Mapping[str, JsonValue] | None) -> str | None:
    if config is None:
        return None
    try:
        encoded = json.dumps(
            plain_json(config), ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
    except (TypeError, ValueError) as error:
        raise ValueError("the database configuration is not plain JSON") from error
    if len(encoded.encode("utf-8")) > MAX_CONFIG_JSON_BYTES:
        raise ValueError(
            f"the database configuration exceeds {MAX_CONFIG_JSON_BYTES} bytes"
        )
    return encoded


def _decode(row: sqlite3.Row) -> WorkspaceDocument:
    config: Mapping[str, JsonValue] | None = None
    raw = row["config_json"]
    try:
        if raw is not None:
            loaded = json.loads(raw)
            if not isinstance(loaded, dict):
                raise ValueError("configuration is not an object")
            config = loaded
        return WorkspaceDocument(
            doc_id=row["doc_id"],
            name=row["name"],
            kind=row["kind"],
            source_type=row["source_type"],
            plugin_id=row["plugin_id"],
            path=row["path"],
            version=row["version"],
            submitted_at=row["submitted_at"],
            config=config,
        )
    except (TypeError, ValueError) as error:
        raise WorkspaceError(
            "the workspace registry contains a malformed document record"
        ) from error


def _require_text(name: str, value: object, limit: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be a non-empty string of at most {limit} characters")


__all__ = [
    "DOCUMENT_KINDS",
    "MAX_CONFIG_JSON_BYTES",
    "MAX_DOCUMENT_NAME_CHARS",
    "MAX_WORKSPACE_DOCUMENTS",
    "WORKSPACE_FILE_NAME",
    "WORKSPACE_SCHEMA_VERSION",
    "WorkspaceDocument",
    "WorkspaceRegistry",
    "validate_document_name",
]
