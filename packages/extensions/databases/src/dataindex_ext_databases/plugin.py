"""Free SQLite extension with schema indexing and bounded approved queries."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from dataindex.models import (
    DataIndexExtension,
    EvidenceKind,
    EvidenceRecord,
    ExtensionManifest,
    HealthResult,
    HealthStatus,
    IndexContext,
    JsonValue,
    NativeLocator,
    Node,
    ProbeResult,
    RetrievalContext,
    RetrievalToolSpec,
    SourceRef,
    ToolRequest,
)

from dataindex_ext_databases.exceptions import (
    ClosedDatabaseIndexError,
    DatabaseDeadlineExceededError,
    DatabaseNotRegisteredError,
    DatabaseQueryError,
    DatabaseRegistrationError,
    DatabaseResultTooLargeError,
    DatabaseSourceChangedError,
    DatabaseSchemaChangedError,
    DatabaseSourceConflictError,
    InvalidDatabaseConfigurationError,
    InvalidDatabaseToolRequestError,
    UnauthorizedDatabaseAccessError,
    UnsupportedDatabaseSourceError,
)
from dataindex_ext_databases.queries import (
    MAX_QUERY_ROWS,
    ColumnRef,
    JoinRule,
    QueryDefinition,
)
from dataindex_ext_databases.sqlite import (
    MAX_SCHEMA_COLUMNS,
    MAX_SCHEMA_TABLES,
    MAX_SQLITE_RESULT_ROWS,
    MAX_SQLITE_VALUE_BYTES,
    SchemaInfo,
    open_read_only,
    inspect_schema,
    query_schema_version,
    run_bounded_query,
)

PLUGIN_ID = "databases"
DISTRIBUTION = "dataindex-ext-databases"
PLUGIN_VERSION = "0.1.0"
CORE_API_RANGE = ">=0.1,<0.2"
SUPPORTED_SOURCE_TYPES = frozenset(
    {"application/vnd.sqlite3", "application/x-sqlite3"}
)
QUERY_TOOL_ID = "databases.query"
MAX_QUERY_TEXT_BYTES = MAX_SQLITE_VALUE_BYTES
MAX_QUERY_DEFINITIONS = 128
MAX_DESCRIPTION_JSON_BYTES = 12 * 1024
MAX_SOURCE_KEY_LENGTH = 256
MAX_REGISTERED_SOURCES = 256

_MANIFEST = ExtensionManifest(
    plugin_id=PLUGIN_ID,
    distribution=DISTRIBUTION,
    plugin_version=PLUGIN_VERSION,
    core_api_range=CORE_API_RANGE,
    edition="free",
    license_mode="none",
    capabilities=("database.index", "database.query"),
    source_types=tuple(sorted(SUPPORTED_SOURCE_TYPES)),
    publisher="DataIndex project",
)

def _query_tool(
    query_ids: Sequence[str] | None = None, description: str | None = None
) -> RetrievalToolSpec:
    query_id_schema: dict[str, JsonValue] = {
        "type": "string",
        "minLength": 1,
        "maxLength": 64,
    }
    if query_ids is not None:
        query_id_schema["enum"] = list(query_ids)
    input_schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": {
            "source_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_SOURCE_KEY_LENGTH,
            },
            "source_version": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_SOURCE_KEY_LENGTH,
            },
            "query_id": query_id_schema,
            "filters": {"type": "object"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_QUERY_ROWS},
        },
        "required": ["source_id", "source_version", "query_id", "filters"],
        "additionalProperties": False,
    }
    if description is not None:
        input_schema["description"] = description
    return RetrievalToolSpec(
        tool_id=QUERY_TOOL_ID,
        input_schema=input_schema,
        capability="database.query",
        maximum_results=MAX_SQLITE_RESULT_ROWS,
    )


_QUERY_TOOL = _query_tool()


@dataclass(frozen=True, slots=True)
class _RegisteredSource:
    source: SourceRef
    path: Path
    file_state_sha256: str
    allowed_columns: Mapping[str, frozenset[str]]
    schema: SchemaInfo
    queries: Mapping[str, QueryDefinition]


class SQLiteDatabaseExtension(DataIndexExtension):
    """Index allowed SQLite schema and execute only host-approved query specs.

    The host authorizes the source and passes its explicit local path to
    ``register_database``. Source IDs are never interpreted as paths. Every
    connection is opened in SQLite read-only mode and every operation is
    restricted to a predeclared ``QueryDefinition``.
    """

    def __init__(self) -> None:
        self._sources: dict[tuple[str, str], _RegisteredSource] = {}
        self._closed = False

    def manifest(self) -> ExtensionManifest:
        return _MANIFEST

    def probe(self, source: SourceRef) -> ProbeResult:
        self._ensure_open()
        if source.source_type not in SUPPORTED_SOURCE_TYPES:
            return ProbeResult(
                False, f"unsupported database source type {source.source_type!r}"
            )
        return ProbeResult(True)

    def register_database(
        self,
        source: SourceRef,
        database_path: str | os.PathLike[str],
        *,
        allowed_columns: Mapping[str, Sequence[str]],
        queries: Sequence[QueryDefinition],
    ) -> None:
        """Register an authorized file and explicit per-table column allowlist."""
        self._ensure_open()
        probe = self.probe(source)
        if not probe.supported:
            raise UnsupportedDatabaseSourceError(probe.reason or "unsupported source")
        if (
            len(source.source_id) > MAX_SOURCE_KEY_LENGTH
            or len(source.version) > MAX_SOURCE_KEY_LENGTH
        ):
            raise InvalidDatabaseConfigurationError(
                f"source ID and version must be at most {MAX_SOURCE_KEY_LENGTH} "
                "characters"
            )
        allowed = _normalize_allowlist(allowed_columns)
        if not isinstance(queries, Sequence) or isinstance(queries, (str, bytes)):
            raise InvalidDatabaseConfigurationError(
                "queries must be a finite sequence of QueryDefinition values"
            )
        if not 1 <= len(queries) <= MAX_QUERY_DEFINITIONS:
            raise InvalidDatabaseConfigurationError(
                f"provide between 1 and {MAX_QUERY_DEFINITIONS} query definitions"
            )
        query_list = tuple(queries)
        if not query_list or any(
            not isinstance(query, QueryDefinition) for query in query_list
        ):
            raise InvalidDatabaseConfigurationError(
                "queries must contain at least one QueryDefinition"
            )
        query_map = {query.query_id: query for query in query_list}
        if len(query_map) != len(query_list):
            raise InvalidDatabaseConfigurationError("query IDs must be unique")
        key = (source.source_id, source.version)
        existing = self._sources.get(key)
        if existing is None and len(self._sources) >= MAX_REGISTERED_SOURCES:
            raise InvalidDatabaseConfigurationError(
                f"at most {MAX_REGISTERED_SOURCES} source revisions may be registered"
            )
        previous = [
            old_source
            for old_source in self._sources.values()
            if old_source.source.source_id == source.source_id
        ]
        if any(old.source.source_type != source.source_type for old in previous):
            raise DatabaseSourceConflictError(
                "a source type cannot change between registered revisions"
            )
        path = _resolve_database_file(database_path)

        file_state_before = _database_file_state_sha256(path)
        connection: sqlite3.Connection | None = None
        try:
            connection = open_read_only(path)
            connection.execute("BEGIN")
            schema = inspect_schema(connection, allowed)
            connection.execute("COMMIT")
        except (OSError, sqlite3.Error, UnauthorizedDatabaseAccessError) as error:
            raise DatabaseRegistrationError(
                "could not inspect the allowlisted SQLite schema"
            ) from error
        finally:
            if connection is not None:
                connection.close()

        file_state_after = _database_file_state_sha256(path)
        if file_state_before != file_state_after:
            raise DatabaseSourceChangedError(
                "SQLite file changed during registration; retry with a new source version"
            )
        for query in query_list:
            _validate_query_definition(query, allowed, schema)
        registered = _RegisteredSource(
            source, path, file_state_after, allowed, schema, query_map
        )
        if existing is not None:
            if (
                existing.path != path
                or existing.file_state_sha256 != file_state_after
                or existing.allowed_columns != allowed
                or existing.schema.version != schema.version
                or existing.queries != query_map
                or existing.source.source_type != source.source_type
            ):
                raise DatabaseSourceConflictError(
                    "the same source ID/version was registered with different "
                    "database configuration or schema"
                )
            return
        self._sources[key] = registered

    def register_database_config(
        self,
        source: SourceRef,
        database_path: str | os.PathLike[str],
        config: Mapping[str, object],
    ) -> None:
        """Register a database from its JSON-compatible configuration.

        ``config`` holds ``allowed_columns`` (table name to column names) and
        ``queries`` (``QueryDefinition.to_mapping`` values). Hosts persist this
        form so a source can be registered again after a restart.
        """
        self._ensure_open()
        if not isinstance(config, Mapping) or set(config) != {
            "allowed_columns",
            "queries",
        }:
            raise InvalidDatabaseConfigurationError(
                "database configuration requires exactly allowed_columns and queries"
            )
        allowed_columns = config["allowed_columns"]
        raw_queries = config["queries"]
        if not isinstance(allowed_columns, Mapping):
            raise InvalidDatabaseConfigurationError(
                "allowed_columns must map tables to column names"
            )
        if not isinstance(raw_queries, (list, tuple)) or not (
            1 <= len(raw_queries) <= MAX_QUERY_DEFINITIONS
        ):
            raise InvalidDatabaseConfigurationError(
                f"queries must be a list of 1 to {MAX_QUERY_DEFINITIONS} definitions"
            )
        try:
            queries = tuple(QueryDefinition.from_mapping(item) for item in raw_queries)
        except (TypeError, ValueError) as error:
            raise InvalidDatabaseConfigurationError(
                f"invalid query definition: {error}"
            ) from error
        self.register_database(
            source, database_path, allowed_columns=allowed_columns, queries=queries
        )

    def database_source_version(
        self, database_path: str | os.PathLike[str]
    ) -> str:
        """Return a source version string for the file's current state.

        The value changes when the SQLite file or its WAL file is replaced,
        resized or modified, so a changed database always needs a new version.
        """
        self._ensure_open()
        return "sqlite-state:" + _database_file_state_sha256(
            _resolve_database_file(database_path)
        )

    def unregister_database(self, source_id: str) -> int:
        """Drop every registered revision of a source; return how many were dropped."""
        self._ensure_open()
        keys = [key for key in self._sources if key[0] == source_id]
        for key in keys:
            del self._sources[key]
        return len(keys)

    def iter_index_records(
        self, source: SourceRef, ctx: IndexContext
    ) -> Iterator[Node]:
        self._ensure_open()
        registered = self._get_source(source)
        self._check_deadline(ctx.deadline_monotonic)
        self._assert_schema_current(registered)
        self._check_deadline(ctx.deadline_monotonic)
        root_locator = NativeLocator(
            "sqlite-schema",
            1,
            {"kind": "database", "schema_version": registered.schema.version},
        )
        root_id = _node_id(source, root_locator)
        yield Node(
            node_id=root_id,
            source_id=source.source_id,
            source_version=source.version,
            kind="database",
            title="SQLite database",
            locator=root_locator,
            attributes={"schema_version": registered.schema.version},
        )
        table_ids: dict[str, str] = {}
        for table_name in sorted(registered.schema.tables):
            table = registered.schema.tables[table_name]
            locator = NativeLocator(
                "sqlite-schema", 1, {"kind": "table", "table": table_name}
            )
            table_id = _node_id(source, locator)
            table_ids[table_name] = table_id
            yield Node(
                node_id=table_id,
                source_id=source.source_id,
                source_version=source.version,
                kind="database_table",
                title=table_name,
                locator=locator,
                parent_id=root_id,
                attributes={
                    "primary_key_columns": (
                        list(table.primary_key_columns)
                        if set(table.primary_key_columns)
                        <= registered.allowed_columns[table_name]
                        else []
                    ),
                    "primary_key_complete": (
                        set(table.primary_key_columns)
                        <= registered.allowed_columns[table_name]
                    ),
                    "primary_key_column_count": len(table.primary_key_columns),
                    "foreign_key_count": len(table.foreign_keys),
                    "column_count": len(table.columns),
                },
            )
            for column in table.columns:
                self._check_deadline(ctx.deadline_monotonic)
                column_locator = NativeLocator(
                    "sqlite-schema",
                    1,
                    {
                        "kind": "column",
                        "table": table_name,
                        "column": column.name,
                    },
                )
                yield Node(
                    node_id=_node_id(source, column_locator),
                    source_id=source.source_id,
                    source_version=source.version,
                    kind="database_column",
                    title=f"{table_name}.{column.name}",
                    locator=column_locator,
                    parent_id=table_id,
                    attributes={
                        "declared_type": column.declared_type,
                        "nullable": not column.not_null,
                        "primary_key_position": column.primary_key_position,
                        "ordinal": column.ordinal,
                    },
                )
        for table_name in sorted(registered.schema.tables):
            table = registered.schema.tables[table_name]
            for foreign_key in table.foreign_keys:
                self._check_deadline(ctx.deadline_monotonic)
                locator = NativeLocator(
                    "sqlite-schema",
                    1,
                    {
                        "kind": "foreign_key",
                        "table": table_name,
                        "constraint_id": foreign_key.constraint_id,
                        "sequence": foreign_key.sequence,
                    },
                )
                yield Node(
                    node_id=_node_id(source, locator),
                    source_id=source.source_id,
                    source_version=source.version,
                    kind="database_foreign_key",
                    title=(
                        f"{foreign_key.from_column} -> "
                        f"{foreign_key.to_table}.{foreign_key.to_column}"
                    ),
                    locator=locator,
                    parent_id=table_ids[table_name],
                    attributes={
                        "relationship_kind": "foreign_key",
                        "from_table": foreign_key.from_table,
                        "from_column": foreign_key.from_column,
                        "to_table": foreign_key.to_table,
                        "to_column": foreign_key.to_column,
                        "target_node_id": table_ids[foreign_key.to_table],
                        "on_update": foreign_key.on_update,
                        "on_delete": foreign_key.on_delete,
                    },
                )

    def tools(self) -> Sequence[RetrievalToolSpec]:
        self._ensure_open()
        return (_QUERY_TOOL,)

    def tools_for_source(
        self, source: SourceRef
    ) -> Sequence[RetrievalToolSpec]:
        self._ensure_open()
        if (source.source_id, source.version) not in self._sources:
            return ()
        registered = self._get_source(source)
        return (
            _query_tool(
                tuple(sorted(registered.queries)),
                _describe_queries(registered.queries),
            ),
        )

    def run_tool(
        self, request: ToolRequest, ctx: RetrievalContext
    ) -> EvidenceRecord:
        self._ensure_open()
        if request.tool_id != QUERY_TOOL_ID:
            raise InvalidDatabaseToolRequestError(
                f"unknown database tool {request.tool_id!r}"
            )
        parameters = request.parameters
        required = {"source_id", "source_version", "query_id", "filters"}
        allowed = required | {"limit"}
        if not required.issubset(parameters) or set(parameters) - allowed:
            raise InvalidDatabaseToolRequestError(
                "databases.query requires source_id, source_version, query_id and "
                "filters; only limit is optional"
            )
        for key in ("source_id", "source_version", "query_id"):
            if not isinstance(parameters[key], str) or not parameters[key].strip():
                raise InvalidDatabaseToolRequestError(
                    f"{key} must be a non-empty string"
                )
        if (
            len(parameters["source_id"]) > MAX_SOURCE_KEY_LENGTH
            or len(parameters["source_version"]) > MAX_SOURCE_KEY_LENGTH
            or len(parameters["query_id"]) > 64
        ):
            raise InvalidDatabaseToolRequestError(
                "source IDs, versions and query IDs exceed their length limits"
            )
        filters = parameters["filters"]
        if not isinstance(filters, Mapping) or not all(
            isinstance(key, str) and len(key) <= 64 for key in filters
        ):
            raise InvalidDatabaseToolRequestError(
                "filters must be an object with short string keys"
            )
        self._check_deadline(ctx.deadline_monotonic)
        source = self._source_for(
            parameters["source_id"], parameters["source_version"]
        )
        registered = self._get_source(source)
        query_id = parameters["query_id"]
        query = registered.queries.get(query_id)
        if query is None:
            raise InvalidDatabaseToolRequestError(
                f"query template {query_id!r} is not approved for this source"
            )
        maximum = min(query.max_rows, ctx.maximum_results, MAX_SQLITE_RESULT_ROWS)
        requested_limit = parameters.get("limit", maximum)
        if (
            not isinstance(requested_limit, int)
            or isinstance(requested_limit, bool)
            or not 1 <= requested_limit <= maximum
        ):
            raise InvalidDatabaseToolRequestError(
                f"limit must be between 1 and {maximum}"
            )

        connection: sqlite3.Connection | None = None
        try:
            connection = open_read_only(registered.path)
            connection.execute("BEGIN")
            _check_schema_version(connection, registered)
            _assert_file_state(registered)
            read_timestamp = _utc_timestamp()
            rows, truncated = run_bounded_query(
                connection,
                query,
                filters,
                requested_limit,
                registered.allowed_columns,
                ctx.deadline_monotonic,
            )
            _check_schema_version(connection, registered)
            self._check_deadline(ctx.deadline_monotonic)
            connection.execute("COMMIT")
        except (sqlite3.Error, OSError) as error:
            raise DatabaseQueryError("could not open or query the SQLite source") from error
        finally:
            if connection is not None:
                connection.close()
        _assert_file_state(registered)

        canonical_filters = json.loads(_canonical_json(dict(filters)))
        query_definition_sha256 = hashlib.sha256(
            _canonical_json(asdict(query)).encode("utf-8")
        ).hexdigest()
        result = {
            "query_id": query.query_id,
            "filters": canonical_filters,
            "effective_limit": requested_limit,
            "query_definition_sha256": query_definition_sha256,
            "database_file_state_sha256": registered.file_state_sha256,
            "read_timestamp_utc": read_timestamp,
            "schema_version": registered.schema.version,
            "row_count": len(rows),
            "truncated": truncated,
            "rows": rows,
        }
        try:
            result_size = len(
                json.dumps(
                    result,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        except (TypeError, ValueError) as error:
            raise DatabaseQueryError(
                "SQLite result could not be represented as JSON evidence"
            ) from error
        if result_size > MAX_QUERY_TEXT_BYTES:
            raise DatabaseResultTooLargeError(
                f"query result exceeds the {MAX_QUERY_TEXT_BYTES}-byte evidence limit"
            )
        return EvidenceRecord(
            source_id=source.source_id,
            source_version=source.version,
            locator=NativeLocator(
                "sqlite-query",
                1,
                {
                    "query_id": query.query_id,
                    "query_definition_sha256": query_definition_sha256,
                    "database_file_state_sha256": registered.file_state_sha256,
                    "read_timestamp_utc": read_timestamp,
                    "schema_version": registered.schema.version,
                },
            ),
            operation=QUERY_TOOL_ID,
            value=result,
            evidence_kind=(
                EvidenceKind.CALCULATED
                if query.aggregate is not None
                else EvidenceKind.OBSERVED
            ),
            execution_id=str(uuid.uuid4()),
            unit=query.unit,
        )

    def health(self) -> HealthResult:
        if self._closed:
            return HealthResult(HealthStatus.UNAVAILABLE, "database index is closed")
        return HealthResult(HealthStatus.HEALTHY)

    def close(self) -> None:
        self._sources.clear()
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise ClosedDatabaseIndexError("database index is closed")

    def _get_source(self, source: SourceRef) -> _RegisteredSource:
        registered = self._sources.get((source.source_id, source.version))
        if registered is None:
            raise DatabaseNotRegisteredError(
                f"database source {source.source_id!r} version "
                f"{source.version!r} is not registered"
            )
        if registered.source.source_type != source.source_type:
            raise DatabaseSourceConflictError(
                "source type does not match the registered database"
            )
        return registered

    def _source_for(self, source_id: str, version: str) -> SourceRef:
        registered = self._sources.get((source_id, version))
        if registered is None:
            raise DatabaseNotRegisteredError(
                f"database source {source_id!r} version {version!r} is not registered"
            )
        return registered.source

    @staticmethod
    def _assert_schema_current(registered: _RegisteredSource) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = open_read_only(registered.path)
            connection.execute("BEGIN")
            _check_schema_version(connection, registered)
            connection.execute("COMMIT")
        except (sqlite3.Error, OSError) as error:
            raise DatabaseQueryError(
                "could not verify the registered SQLite schema"
            ) from error
        finally:
            if connection is not None:
                connection.close()
        _assert_file_state(registered)

    @staticmethod
    def _check_deadline(deadline: float | None) -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise DatabaseDeadlineExceededError(
                "database operation exceeded its cooperative deadline"
            )


def _resolve_database_file(database_path: str | os.PathLike[str]) -> Path:
    try:
        path = Path(database_path).resolve(strict=True)
    except (OSError, TypeError, ValueError) as error:
        raise DatabaseRegistrationError(
            "database path must identify an existing local file"
        ) from error
    if not path.is_file():
        raise DatabaseRegistrationError("database path must identify a file")
    return path


def _describe_queries(queries: Mapping[str, QueryDefinition]) -> str:
    header = (
        "Run one approved read-only query. Set query_id to a listed ID and give "
        "filters as an object holding exactly the filter names listed for it "
        "(a list of values for 'in' filters)."
    )
    lines = [header]
    used = len(json.dumps(header, ensure_ascii=True))
    for position, query_id in enumerate(sorted(queries)):
        line = f"- {query_id}: {_describe_query(queries[query_id])}"
        size = len(json.dumps(line, ensure_ascii=True)) + 2
        if used + size > MAX_DESCRIPTION_JSON_BYTES:
            lines.append(
                f"- {len(queries) - position} more approved queries are not listed"
            )
            break
        lines.append(line)
        used += size
    return "\n".join(lines)


def _describe_query(query: QueryDefinition) -> str:
    tables = [query.table, *(join.right.table for join in query.joins)]
    returned = [f"{column.table}.{column.column}" for column in query.columns[:12]]
    if len(query.columns) > 12:
        returned.append("more columns")
    if query.aggregate is not None:
        argument = (
            "*"
            if query.aggregate.column is None
            else f"{query.aggregate.column.table}.{query.aggregate.column.column}"
        )
        returned.append(
            f"{query.aggregate.function.upper()}({argument}) AS {query.aggregate.alias}"
        )
    parts = [f"from {', '.join(tables)}", f"returns {', '.join(returned)}"]
    if query.filters:
        parts.append(
            "filters: "
            + ", ".join(
                f"{rule.name} ({rule.operator} {rule.column.table}.{rule.column.column})"
                for rule in query.filters
            )
        )
    else:
        parts.append("filters: none, pass {}")
    if query.unit:
        parts.append(f"unit: {query.unit}")
    parts.append(f"at most {query.max_rows} rows")
    return "; ".join(parts)


def _normalize_allowlist(
    allowed_columns: Mapping[str, Sequence[str]],
) -> dict[str, frozenset[str]]:
    if not isinstance(allowed_columns, Mapping) or not allowed_columns:
        raise InvalidDatabaseConfigurationError(
            "allowed_columns must map at least one table to columns"
        )
    if len(allowed_columns) > MAX_SCHEMA_TABLES:
        raise InvalidDatabaseConfigurationError(
            f"at most {MAX_SCHEMA_TABLES} tables may be allowlisted"
        )
    normalized: dict[str, frozenset[str]] = {}
    total_columns = 0
    for table, columns in allowed_columns.items():
        if not isinstance(table, str) or not table:
            raise InvalidDatabaseConfigurationError(
                "allowlisted table names must be non-empty strings"
            )
        if len(table) > 128:
            raise InvalidDatabaseConfigurationError(
                "allowlisted table names must be at most 128 characters"
            )
        if table.casefold().startswith("sqlite_"):
            raise InvalidDatabaseConfigurationError(
                "SQLite internal tables cannot be allowlisted"
            )
        if not isinstance(columns, Sequence) or isinstance(columns, (str, bytes)):
            raise InvalidDatabaseConfigurationError(
                f"columns for {table!r} must be a sequence of names"
            )
        column_names = tuple(columns)
        if not column_names or any(
            not isinstance(column, str) or not column or len(column) > 128
            for column in column_names
        ):
            raise InvalidDatabaseConfigurationError(
                f"columns for {table!r} must be non-empty strings of at most "
                "128 characters"
            )
        if len(set(column_names)) != len(column_names):
            raise InvalidDatabaseConfigurationError(
                f"columns for {table!r} must be unique"
            )
        normalized[table] = frozenset(column_names)
        total_columns += len(column_names)
        if total_columns > MAX_SCHEMA_COLUMNS:
            raise InvalidDatabaseConfigurationError(
                f"at most {MAX_SCHEMA_COLUMNS} columns may be allowlisted"
            )
    return normalized


def _validate_query_definition(
    query: QueryDefinition,
    allowed_columns: Mapping[str, frozenset[str]],
    schema: SchemaInfo,
) -> None:
    visited_tables = {query.table}
    if query.table not in allowed_columns:
        raise UnauthorizedDatabaseAccessError(
            f"query template {query.query_id!r} uses a non-allowlisted table"
        )
    for join in query.joins:
        if join.left.table not in visited_tables or join.right.table in visited_tables:
            raise InvalidDatabaseConfigurationError(
                f"query template {query.query_id!r} has a disconnected join"
            )
        if join.right.table not in allowed_columns:
            raise UnauthorizedDatabaseAccessError(
                f"query template {query.query_id!r} joins a non-allowlisted table"
            )
        if not _is_declared_foreign_key(join, schema):
            raise InvalidDatabaseConfigurationError(
                f"query template {query.query_id!r} join is not a declared foreign key"
            )
        visited_tables.add(join.right.table)

    references = list(query.columns) + list(query.group_by) + list(query.order_by)
    references.extend(rule.column for rule in query.filters)
    references.extend(rule.column for rule in query.source_filters)
    references.extend(
        join_endpoint
        for join in query.joins
        for join_endpoint in (join.left, join.right)
    )
    if query.aggregate is not None and query.aggregate.column is not None:
        references.append(query.aggregate.column)
    for reference in references:
        if reference.table not in visited_tables:
            raise InvalidDatabaseConfigurationError(
                f"query template {query.query_id!r} references an unjoined table"
            )
        if reference.column not in allowed_columns.get(reference.table, frozenset()):
            raise UnauthorizedDatabaseAccessError(
                f"query template {query.query_id!r} references a non-allowlisted "
                f"column in {reference.table!r}"
            )
    if query.aggregate is None:
        for table_name in visited_tables:
            primary_key = schema.tables[table_name].primary_key_columns
            if not primary_key or any(
                ColumnRef(table_name, column) not in query.order_by
                for column in primary_key
            ):
                raise InvalidDatabaseConfigurationError(
                    f"query template {query.query_id!r} must order by every joined "
                    "table primary key"
                )


def _is_declared_foreign_key(join: JoinRule, schema: SchemaInfo) -> bool:
    for from_table, from_column, to_table, to_column in (
        (join.left.table, join.left.column, join.right.table, join.right.column),
        (join.right.table, join.right.column, join.left.table, join.left.column),
    ):
        table = schema.tables.get(from_table)
        if table is None:
            continue
        matching = tuple(
            foreign_key
            for foreign_key in table.foreign_keys
            if foreign_key.from_table == from_table
            and foreign_key.from_column == from_column
            and foreign_key.to_table == to_table
            and foreign_key.to_column == to_column
        )
        if not matching:
            continue
        constraint = tuple(
            foreign_key
            for foreign_key in table.foreign_keys
            if foreign_key.constraint_id == matching[0].constraint_id
        )
        if len(constraint) == 1:
            return True
    return False


def _check_schema_version(
    connection: sqlite3.Connection, registered: _RegisteredSource
) -> None:
    if query_schema_version(connection) != registered.schema.version:
        raise DatabaseSchemaChangedError(
            "SQLite schema changed; re-register with a new source version"
        )


def _assert_file_state(registered: _RegisteredSource) -> None:
    if _database_file_state_sha256(registered.path) != registered.file_state_sha256:
        raise DatabaseSourceChangedError(
            "SQLite file identity changed; register the changed data with a new "
            "source version"
        )


def _database_file_state_sha256(path: Path) -> str:
    def stat_fields(candidate: Path) -> tuple[int, int, int, int] | None:
        try:
            file_stat = candidate.stat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise DatabaseSourceChangedError(
                "could not verify the registered SQLite file identity"
            ) from error
        return (
            file_stat.st_dev,
            file_stat.st_ino,
            file_stat.st_size,
            file_stat.st_mtime_ns,
        )

    state = {
        "database": stat_fields(path),
        "wal": stat_fields(Path(f"{path}-wal")),
    }
    if state["database"] is None:
        raise DatabaseSourceChangedError(
            "registered SQLite database file is no longer available"
        )
    return hashlib.sha256(_canonical_json(state).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _node_id(source: SourceRef, locator: NativeLocator) -> str:
    identity = json.dumps(
        {
            "source_id": source.source_id,
            "source_version": source.version,
            "scheme": locator.scheme,
            "locator_version": locator.locator_version,
            "value": locator.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "db-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _utc_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def load_plugin() -> SQLiteDatabaseExtension:
    return SQLiteDatabaseExtension()
