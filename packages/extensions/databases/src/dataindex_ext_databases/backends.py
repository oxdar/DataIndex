"""Adapters that give every database engine the same registration contract.

``plugin.py`` validates allowlists and approved queries, builds catalog nodes
and evidence, and delegates engine-specific work to one of these backends:

* ``SQLiteFile`` and ``DuckDBFile`` read a local file. Their source version
  follows the file state, and the schema and file state are re-checked before
  and after every query.
* ``ServerDatabase`` reads a database server through SQLAlchemy or pymongo.
  Its source version follows the reflected schema of the allowlisted tables,
  which is verified at registration and indexing. Server rows can change at any
  time, so evidence records the read timestamp.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from dataindex.models import JsonValue

from dataindex_ext_databases import duckdb_backend
from dataindex_ext_databases.connections import ConnectionSettings
from dataindex_ext_databases.exceptions import (
    DatabaseDeadlineExceededError,
    DatabaseQueryError,
    DatabaseRegistrationError,
    DatabaseSchemaChangedError,
    DatabaseSourceChangedError,
    UnauthorizedDatabaseAccessError,
)
from dataindex_ext_databases.queries import QueryDefinition
from dataindex_ext_databases.results import JsonScalar
from dataindex_ext_databases.schema import SchemaInfo
from dataindex_ext_databases.sqlite import (
    NO_VIEWS,
    inspect_schema,
    inspect_view_access,
    open_read_only,
    query_schema_version,
    run_bounded_query,
)

Rows = list[dict[str, JsonScalar]]
Allowlist = Mapping[str, frozenset[str]]


@dataclass(frozen=True, slots=True)
class QueryOutcome:
    rows: Rows
    truncated: bool
    read_timestamp_utc: str


class DatabaseBackend(Protocol):
    engine: str
    title: str
    schema_scheme: str
    query_scheme: str
    supports_joins: bool
    reserved_prefixes: tuple[str, ...]

    def identity(self) -> tuple[object, ...]:
        """What must match for two registrations of one revision to agree."""

    def inspect(self, allowed: Allowlist, deadline: float | None) -> SchemaInfo:
        """Read the allowlisted schema; raise ``DatabaseRegistrationError``."""

    def assert_current(
        self, schema: SchemaInfo, allowed: Allowlist, deadline: float | None
    ) -> None:
        """Raise when the source no longer matches its registered schema."""

    def run(
        self,
        query: QueryDefinition,
        filters: Mapping[str, object],
        limit: int,
        allowed: Allowlist,
        schema: SchemaInfo,
        deadline: float | None,
    ) -> QueryOutcome:
        """Run one approved query against the registered revision."""

    def root_attributes(self, schema: SchemaInfo) -> dict[str, JsonValue]:
        """Attributes of the database node."""

    def evidence_fields(self) -> dict[str, JsonValue]:
        """Source-state fields placed in evidence values and locators."""


class _Server(Protocol):
    supports_joins: bool

    def inspect(self, allowed: Allowlist, deadline: float | None) -> SchemaInfo: ...

    def run(
        self,
        query: QueryDefinition,
        filters: Mapping[str, object],
        limit: int,
        deadline: float | None,
    ) -> tuple[Rows, bool, str]: ...


class SQLiteFile:
    engine = "sqlite"
    title = "SQLite database"
    schema_scheme = "sqlite-schema"
    query_scheme = "sqlite-query"
    supports_joins = True
    reserved_prefixes = ("sqlite_",)

    def __init__(self, path: Path) -> None:
        self.path = path
        self.file_state_sha256 = ""
        self.view_access = NO_VIEWS

    def identity(self) -> tuple[object, ...]:
        return ("sqlite", self.path, self.file_state_sha256)

    def inspect(self, allowed: Allowlist, deadline: float | None) -> SchemaInfo:
        file_state_before = sqlite_file_state_sha256(self.path)
        connection: sqlite3.Connection | None = None
        try:
            connection = open_read_only(self.path)
            connection.execute("BEGIN")
            schema = inspect_schema(connection, allowed)
            view_access = inspect_view_access(
                connection,
                [name for name, table in schema.tables.items() if table.object_kind == "view"],
            )
            connection.execute("COMMIT")
        except (OSError, sqlite3.Error, UnauthorizedDatabaseAccessError) as error:
            detail = (
                f": {error}" if isinstance(error, UnauthorizedDatabaseAccessError) else ""
            )
            raise DatabaseRegistrationError(
                f"could not inspect the allowlisted SQLite schema{detail}"
            ) from error
        finally:
            if connection is not None:
                connection.close()
        file_state_after = sqlite_file_state_sha256(self.path)
        if file_state_before != file_state_after:
            raise DatabaseSourceChangedError(
                "SQLite file changed during registration; retry with a new source version"
            )
        self.file_state_sha256 = file_state_after
        self.view_access = view_access
        return schema

    def assert_current(
        self, schema: SchemaInfo, allowed: Allowlist, deadline: float | None
    ) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = open_read_only(self.path)
            connection.execute("BEGIN")
            self._check_schema_version(connection, schema)
            connection.execute("COMMIT")
        except (sqlite3.Error, OSError) as error:
            raise DatabaseQueryError(
                "could not verify the registered SQLite schema"
            ) from error
        finally:
            if connection is not None:
                connection.close()
        self._assert_file_state()

    def run(
        self,
        query: QueryDefinition,
        filters: Mapping[str, object],
        limit: int,
        allowed: Allowlist,
        schema: SchemaInfo,
        deadline: float | None,
    ) -> QueryOutcome:
        connection: sqlite3.Connection | None = None
        try:
            connection = open_read_only(self.path)
            connection.execute("BEGIN")
            self._check_schema_version(connection, schema)
            self._assert_file_state()
            read_timestamp = utc_timestamp()
            rows, truncated = run_bounded_query(
                connection,
                query,
                filters,
                limit,
                allowed,
                deadline,
                self.view_access,
            )
            self._check_schema_version(connection, schema)
            check_deadline(deadline)
            connection.execute("COMMIT")
        except (sqlite3.Error, OSError) as error:
            raise DatabaseQueryError("could not open or query the SQLite source") from error
        finally:
            if connection is not None:
                connection.close()
        self._assert_file_state()
        return QueryOutcome(rows, truncated, read_timestamp)

    def root_attributes(self, schema: SchemaInfo) -> dict[str, JsonValue]:
        return {"schema_version": schema.version}

    def evidence_fields(self) -> dict[str, JsonValue]:
        return {"database_file_state_sha256": self.file_state_sha256}

    @staticmethod
    def _check_schema_version(
        connection: sqlite3.Connection, schema: SchemaInfo
    ) -> None:
        if query_schema_version(connection) != schema.version:
            raise DatabaseSchemaChangedError(
                "SQLite schema changed; re-register with a new source version"
            )

    def _assert_file_state(self) -> None:
        if sqlite_file_state_sha256(self.path) != self.file_state_sha256:
            raise DatabaseSourceChangedError(
                "SQLite file identity changed; register the changed data with a new "
                "source version"
            )


class DuckDBFile:
    engine = "duckdb"
    title = "DuckDB database"
    schema_scheme = "duckdb-schema"
    query_scheme = "duckdb-query"
    supports_joins = True
    reserved_prefixes = ("duckdb_", "sqlite_", "pg_")

    def __init__(self, path: Path) -> None:
        self.path = path
        self.file_state_sha256 = ""

    def identity(self) -> tuple[object, ...]:
        return ("duckdb", self.path, self.file_state_sha256)

    def inspect(self, allowed: Allowlist, deadline: float | None) -> SchemaInfo:
        duckdb = duckdb_backend.duckdb_module()
        file_state_before = duckdb_file_state_sha256(self.path)
        connection = None
        try:
            connection = duckdb_backend.open_read_only(self.path)
            schema = duckdb_backend.inspect_schema(connection, allowed)
        except (OSError, duckdb.Error, UnauthorizedDatabaseAccessError) as error:
            detail = (
                f": {error}" if isinstance(error, UnauthorizedDatabaseAccessError) else ""
            )
            raise DatabaseRegistrationError(
                f"could not inspect the allowlisted DuckDB schema{detail}"
            ) from error
        finally:
            if connection is not None:
                connection.close()
        file_state_after = duckdb_file_state_sha256(self.path)
        if file_state_before != file_state_after:
            raise DatabaseSourceChangedError(
                "DuckDB file changed during registration; retry with a new source version"
            )
        self.file_state_sha256 = file_state_after
        return schema

    def assert_current(
        self, schema: SchemaInfo, allowed: Allowlist, deadline: float | None
    ) -> None:
        duckdb = duckdb_backend.duckdb_module()
        self._assert_file_state()
        connection = None
        try:
            connection = duckdb_backend.open_read_only(self.path)
            digest = duckdb_backend.schema_digest(connection, allowed)
        except (OSError, duckdb.Error) as error:
            raise DatabaseQueryError(
                "could not verify the registered DuckDB schema"
            ) from error
        finally:
            if connection is not None:
                connection.close()
        if digest != schema.version:
            raise DatabaseSchemaChangedError(
                "DuckDB schema changed; re-register with a new source version"
            )
        self._assert_file_state()

    def run(
        self,
        query: QueryDefinition,
        filters: Mapping[str, object],
        limit: int,
        allowed: Allowlist,
        schema: SchemaInfo,
        deadline: float | None,
    ) -> QueryOutcome:
        self._assert_file_state()
        read_timestamp = utc_timestamp()
        try:
            rows, truncated = duckdb_backend.run_query(
                self.path, schema.version, query, filters, limit, allowed, deadline
            )
        except OSError as error:
            raise DatabaseQueryError("could not open or query the DuckDB source") from error
        check_deadline(deadline)
        self._assert_file_state()
        return QueryOutcome(rows, truncated, read_timestamp)

    def root_attributes(self, schema: SchemaInfo) -> dict[str, JsonValue]:
        return {"schema_version": schema.version, "engine": self.engine}

    def evidence_fields(self) -> dict[str, JsonValue]:
        return {
            "engine": self.engine,
            "database_file_state_sha256": self.file_state_sha256,
        }

    def _assert_file_state(self) -> None:
        if duckdb_file_state_sha256(self.path) != self.file_state_sha256:
            raise DatabaseSourceChangedError(
                "DuckDB file identity changed; register the changed data with a new "
                "source version"
            )


class ServerDatabase:
    """A database server reached through SQLAlchemy or pymongo."""

    reserved_prefixes: tuple[str, ...] = ()

    def __init__(self, settings: ConnectionSettings) -> None:
        self.settings = settings
        self.engine = settings.engine.name
        self.title = f"{settings.engine.label} database"
        self.schema_scheme = f"{self.engine}-schema"
        self.query_scheme = f"{self.engine}-query"
        self.server: _Server
        if settings.engine.family == "mongodb":
            from dataindex_ext_databases.mongodb_backend import MongoServer

            self.server = MongoServer(settings)
        else:
            from dataindex_ext_databases.sqlalchemy_backend import SQLAlchemyServer

            self.server = SQLAlchemyServer(settings)
        self.supports_joins = self.server.supports_joins
        if self.engine == "mongodb":
            self.reserved_prefixes = ("system.",)

    def identity(self) -> tuple[object, ...]:
        return (
            self.engine,
            self.settings.url,
            self.settings.schema,
            self.settings.password_env,
        )

    def inspect(self, allowed: Allowlist, deadline: float | None) -> SchemaInfo:
        try:
            return self.server.inspect(allowed, deadline)
        except UnauthorizedDatabaseAccessError as error:
            raise DatabaseRegistrationError(
                f"could not inspect the allowlisted {self.settings.engine.label} "
                f"schema: {error}"
            ) from error

    def assert_current(
        self, schema: SchemaInfo, allowed: Allowlist, deadline: float | None
    ) -> None:
        try:
            current = self.server.inspect(allowed, deadline)
        except UnauthorizedDatabaseAccessError as error:
            raise DatabaseSchemaChangedError(
                f"{self.settings.engine.label} schema changed: {error}"
            ) from error
        if current.version != schema.version:
            raise DatabaseSchemaChangedError(
                f"{self.settings.engine.label} schema changed; submit the "
                "connection again"
            )

    def run(
        self,
        query: QueryDefinition,
        filters: Mapping[str, object],
        limit: int,
        allowed: Allowlist,
        schema: SchemaInfo,
        deadline: float | None,
    ) -> QueryOutcome:
        rows, truncated, read_timestamp = self.server.run(
            query, filters, limit, deadline
        )
        return QueryOutcome(rows, truncated, read_timestamp)

    def root_attributes(self, schema: SchemaInfo) -> dict[str, JsonValue]:
        return {
            "schema_version": schema.version,
            "engine": self.engine,
            "connection_id": self.settings.connection_id,
            "support_tier": self.settings.engine.tier,
        }

    def evidence_fields(self) -> dict[str, JsonValue]:
        return {"engine": self.engine, "connection_id": self.settings.connection_id}


def sqlite_file_state_sha256(path: Path) -> str:
    return _file_state_sha256(path, Path(f"{path}-wal"), "SQLite")


def duckdb_file_state_sha256(path: Path) -> str:
    return _file_state_sha256(path, Path(f"{path}.wal"), "DuckDB")


def _file_state_sha256(path: Path, wal: Path, label: str) -> str:
    def stat_fields(candidate: Path) -> tuple[int, int, int, int] | None:
        try:
            file_stat = candidate.stat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise DatabaseSourceChangedError(
                f"could not verify the registered {label} file identity"
            ) from error
        return (
            file_stat.st_dev,
            file_stat.st_ino,
            file_stat.st_size,
            file_stat.st_mtime_ns,
        )

    state = {"database": stat_fields(path), "wal": stat_fields(wal)}
    if state["database"] is None:
        raise DatabaseSourceChangedError(
            f"registered {label} database file is no longer available"
        )
    return hashlib.sha256(canonical_json(state).encode("utf-8")).hexdigest()


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def utc_timestamp() -> str:
    return (
        datetime.now(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise DatabaseDeadlineExceededError(
            "database operation exceeded its cooperative deadline"
        )
