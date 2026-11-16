"""Read-only DuckDB database files through the native ``duckdb`` package.

A DuckDB file is treated like a SQLite file: the host submits a local path, the
source version follows the file (and its ``.wal``) state, and the schema digest
plus file state are checked before and after every query.

Each operation opens a fresh connection in read-only mode with external access,
extension loading, Python replacement scans and further configuration changes
disabled, one thread and a bounded memory limit. Long queries are interrupted at
the operation deadline. Base tables and views in the ``main`` schema of the
file can be allowlisted.

A view runs code from the file when it is read, so its definition is checked
at registration: it may read only tables and views of the file's ``main``
schema, and may call only built-in functions DuckDB marks as consistent (the
same inputs always give the same result). Table functions, macros, sampling,
time travel and functions that reveal settings, variables or the clock are
refused. The same function rule applies to generated columns of the tables an
allowlisted table or view reads.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from dataindex_ext_databases.exceptions import (
    DatabaseDeadlineExceededError,
    DatabaseDriverUnavailableError,
    DatabaseQueryError,
    DatabaseSchemaChangedError,
    UnauthorizedDatabaseAccessError,
)
from dataindex_ext_databases.queries import QueryDefinition, build_query, output_keys
from dataindex_ext_databases.results import JsonScalar, collect_rows
from dataindex_ext_databases.schema import (
    MAX_COLUMNS_PER_TABLE,
    MAX_DECLARED_TYPE_CHARS,
    MAX_SCHEMA_FOREIGN_KEYS,
    ColumnInfo,
    ForeignKeyInfo,
    SchemaInfo,
    TableInfo,
)

DUCKDB_SOURCE_TYPE = "application/vnd.duckdb"
DUCKDB_MAGIC = b"DUCK"
DUCKDB_MEMORY_LIMIT = "256MB"
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_VIEW_DEPENDENCIES = 64
MAX_VIEW_DEFINITION_CHARS = 100_000
MAX_VIEW_SYNTAX_NODES = 50_000
_CONNECTION_CONFIG = {
    "enable_external_access": False,
    "autoinstall_known_extensions": False,
    "autoload_known_extensions": False,
    "allow_community_extensions": False,
    "python_enable_replacements": False,
    "threads": 1,
    "memory_limit": DUCKDB_MEMORY_LIMIT,
    "lock_configuration": True,
}
_VIEW_TABLE_REFERENCES = frozenset(
    {"BASE_TABLE", "JOIN", "SUBQUERY", "EXPRESSION_LIST", "EMPTY"}
)
_VIEW_FUNCTION_TYPES = frozenset({"scalar", "aggregate"})
# Built-in window operators; only DuckDB 2.0+ also lists them in duckdb_functions().
_VIEW_WINDOW_TYPES = frozenset(
    {
        "WINDOW_CUME_DIST",
        "WINDOW_FILL",
        "WINDOW_FIRST_VALUE",
        "WINDOW_LAG",
        "WINDOW_LAST_VALUE",
        "WINDOW_LEAD",
        "WINDOW_NTH_VALUE",
        "WINDOW_NTILE",
        "WINDOW_PERCENT_RANK",
        "WINDOW_RANK",
        "WINDOW_RANK_DENSE",
        "WINDOW_ROW_NUMBER",
    }
)
# DuckDB marks some of these consistent although they read the session or the clock.
_VIEW_REFUSED_FUNCTIONS = frozenset(
    {
        "current_localtime",
        "current_localtimestamp",
        "current_setting",
        "getenv",
        "getvariable",
    }
)
_IDENTIFIER = r'(?:"(?:[^"]|"")*"|[^\s"().,;]+)'
_CREATE_VIEW = re.compile(
    rf"\s*CREATE\s+VIEW\s+(?:{_IDENTIFIER}\s*\.\s*){{0,2}}(?P<name>{_IDENTIFIER})"
    rf"(?:\s*\(\s*{_IDENTIFIER}(?:\s*,\s*{_IDENTIFIER})*\s*\))?\s+AS\s+",
    re.IGNORECASE,
)


def is_duckdb_file(path: Path) -> bool:
    """True when the file starts with a DuckDB storage header."""
    try:
        with path.open("rb") as handle:
            header = handle.read(12)
    except OSError:
        return False
    return len(header) == 12 and header[8:12] == DUCKDB_MAGIC


def duckdb_module() -> Any:
    try:
        import duckdb
    except ImportError:
        raise DatabaseDriverUnavailableError(
            "DuckDB support needs the duckdb package; run: "
            'pip install "dataindex-ext-databases[duckdb]"'
        ) from None
    return duckdb


def open_read_only(path: Path) -> Any:
    duckdb = duckdb_module()
    return duckdb.connect(str(path), read_only=True, config=dict(_CONNECTION_CONFIG))


def inspect_schema(
    connection: Any, allowed_columns: Mapping[str, frozenset[str]]
) -> SchemaInfo:
    """Describe the allowlisted tables and views; ``version`` digests their catalog."""
    catalog = _read_catalog(connection, allowed_columns)
    view_definitions = dict(catalog["views"])
    missing_tables = sorted(
        set(allowed_columns) - set(catalog["tables"]) - set(view_definitions)
    )
    if missing_tables:
        raise UnauthorizedDatabaseAccessError(
            f"allowlisted tables or views are absent: {missing_tables}"
        )
    _ViewChecker(connection).check(allowed_columns, view_definitions)
    columns_by_table: dict[str, list[tuple[Any, ...]]] = {}
    for row in catalog["columns"]:
        columns_by_table.setdefault(row[0], []).append(row)
    primary_keys: dict[str, tuple[str, ...]] = {}
    foreign_rows: dict[str, list[tuple[Any, ...]]] = {}
    for row in catalog["constraints"]:
        table_name, _index, kind, column_names, _target, _target_columns = row
        if kind == "PRIMARY KEY":
            primary_keys[table_name] = tuple(column_names)
        else:
            foreign_rows.setdefault(table_name, []).append(row)

    tables: dict[str, TableInfo] = {}
    foreign_key_count = 0
    for table_name, permitted in allowed_columns.items():
        rows = columns_by_table.get(table_name, [])
        if len(rows) > MAX_COLUMNS_PER_TABLE:
            raise UnauthorizedDatabaseAccessError(
                f"table {table_name!r} exceeds the supported schema-column limit"
            )
        primary_key = primary_keys.get(table_name, ())
        available = {
            row[1]: ColumnInfo(
                name=row[1],
                declared_type=str(row[3] or "")[:MAX_DECLARED_TYPE_CHARS],
                not_null=not bool(row[4]),
                primary_key_position=(
                    primary_key.index(row[1]) + 1 if row[1] in primary_key else 0
                ),
                ordinal=int(row[2]) - 1,
            )
            for row in rows
        }
        missing_columns = sorted(permitted - set(available))
        if missing_columns:
            raise UnauthorizedDatabaseAccessError(
                f"allowlisted columns are absent from {table_name!r}: "
                f"{missing_columns}"
            )
        foreign_keys: list[ForeignKeyInfo] = []
        for row in foreign_rows.get(table_name, []):
            _table, constraint_index, _kind, from_columns, target, to_columns = row
            foreign_key_count += 1
            if foreign_key_count > MAX_SCHEMA_FOREIGN_KEYS:
                raise UnauthorizedDatabaseAccessError(
                    f"allowlisted schema exceeds the {MAX_SCHEMA_FOREIGN_KEYS}-"
                    "foreign-key metadata limit"
                )
            if (
                target not in allowed_columns
                or len(from_columns) != len(to_columns)
                or any(column not in permitted for column in from_columns)
                or any(column not in allowed_columns[target] for column in to_columns)
            ):
                continue
            foreign_keys.extend(
                ForeignKeyInfo(
                    constraint_id=int(constraint_index),
                    sequence=sequence,
                    from_table=table_name,
                    from_column=from_column,
                    to_table=target,
                    to_column=to_column,
                    on_update="NO ACTION",
                    on_delete="NO ACTION",
                )
                for sequence, (from_column, to_column) in enumerate(
                    zip(from_columns, to_columns, strict=True)
                )
            )
        tables[table_name] = TableInfo(
            name=table_name,
            columns=tuple(
                available[row[1]] for row in rows if row[1] in permitted
            ),
            foreign_keys=tuple(foreign_keys),
            primary_key_columns=primary_key,
            object_kind="view" if table_name in view_definitions else "table",
        )
    return SchemaInfo(catalog["digest"], tables)


def schema_digest(
    connection: Any, allowed_columns: Mapping[str, frozenset[str]]
) -> str:
    return str(_read_catalog(connection, allowed_columns)["digest"])


def run_query(
    path: Path,
    schema_version: int | str,
    query: QueryDefinition,
    filters: Mapping[str, object],
    limit: int,
    allowed_columns: Mapping[str, frozenset[str]],
    deadline_monotonic: float | None,
) -> tuple[list[dict[str, JsonScalar]], bool]:
    """Run one approved query; the SQL comes only from ``build_query``."""
    statement, parameters = build_query(query, filters, limit)
    duckdb = duckdb_module()
    remaining = _remaining(deadline_monotonic)
    connection: Any = None
    timer: threading.Timer | None = None
    try:
        connection = open_read_only(path)
        timer = threading.Timer(remaining, connection.interrupt)
        timer.daemon = True
        timer.start()
        _check_digest(connection, allowed_columns, schema_version)
        cursor = connection.execute(statement, list(parameters))
        records = cursor.fetchmany(limit + 1)
        _check_digest(connection, allowed_columns, schema_version)
    except duckdb.InterruptException:
        raise DatabaseDeadlineExceededError(
            "DuckDB query exceeded its deadline"
        ) from None
    except duckdb.Error as error:
        raise DatabaseQueryError(
            f"approved DuckDB query failed: {type(error).__name__}"
        ) from error
    finally:
        if timer is not None:
            timer.cancel()
        if connection is not None:
            connection.close()
    return collect_rows(records, output_keys(query), limit)


def _check_digest(
    connection: Any,
    allowed_columns: Mapping[str, frozenset[str]],
    expected: int | str,
) -> None:
    if schema_digest(connection, allowed_columns) != expected:
        raise DatabaseSchemaChangedError(
            "DuckDB schema changed; register the database again"
        )


def _remaining(deadline_monotonic: float | None) -> float:
    if deadline_monotonic is None:
        return DEFAULT_TIMEOUT_SECONDS
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        raise DatabaseDeadlineExceededError(
            "database operation exceeded its cooperative deadline"
        )
    return min(remaining, DEFAULT_TIMEOUT_SECONDS)


def _read_catalog(
    connection: Any, allowed_columns: Mapping[str, frozenset[str]]
) -> dict[str, Any]:
    names = sorted(allowed_columns)
    placeholders = ",".join("?" for _ in names)
    scope = "database_name = current_database() AND schema_name = 'main'"
    tables = [
        row[0]
        for row in connection.execute(
            f"SELECT table_name FROM duckdb_tables() WHERE {scope} "
            f"AND NOT temporary AND table_name IN ({placeholders}) "
            "ORDER BY table_name",
            names,
        ).fetchall()
    ]
    views = _rows(
        connection.execute(
            f"SELECT view_name, sql FROM duckdb_views() WHERE {scope} "
            f"AND NOT temporary AND NOT internal AND view_name IN ({placeholders}) "
            "ORDER BY view_name",
            names,
        ).fetchall()
    )
    columns = _rows(
        connection.execute(
            "SELECT table_name, column_name, column_index, data_type, is_nullable "
            f"FROM duckdb_columns() WHERE {scope} AND table_name IN ({placeholders}) "
            "ORDER BY table_name, column_index",
            names,
        ).fetchmany(MAX_COLUMNS_PER_TABLE * len(names) + 1)
    )
    constraints = _rows(
        connection.execute(
            "SELECT table_name, constraint_index, constraint_type, "
            "constraint_column_names, referenced_table, referenced_column_names "
            f"FROM duckdb_constraints() WHERE {scope} "
            f"AND table_name IN ({placeholders}) "
            "AND constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY') "
            "ORDER BY table_name, constraint_index",
            names,
        ).fetchmany(MAX_SCHEMA_FOREIGN_KEYS + len(names) + 1)
    )
    catalog = {
        "tables": tables,
        "views": views,
        "columns": columns,
        "constraints": constraints,
    }
    digest = hashlib.sha256(
        json.dumps(catalog, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()
    catalog["digest"] = digest
    return catalog


def _rows(records: Sequence[Sequence[Any]]) -> list[tuple[Any, ...]]:
    return [
        tuple(list(value) if isinstance(value, (list, tuple)) else value for value in row)
        for row in records
    ]


class _ViewChecker:
    """Check what reading an allowlisted view or generated column would run."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection
        self._database = str(
            connection.execute("SELECT current_database()").fetchone()[0]
        )
        self._functions: dict[str, bool] = {}

    def check(
        self,
        allowed_columns: Mapping[str, frozenset[str]],
        view_definitions: Mapping[str, str],
    ) -> None:
        # Generated columns to check per table; None checks all of them.
        generated: dict[str, frozenset[str] | None] = {
            name: columns
            for name, columns in allowed_columns.items()
            if name not in view_definitions
        }
        pending = [(name, name, sql) for name, sql in sorted(view_definitions.items())]
        checked: set[str] = set()
        while pending:
            root, view, sql = pending.pop()
            if view.casefold() in checked:
                continue
            checked.add(view.casefold())
            subject = f"view {root!r}"
            if len(checked) > MAX_VIEW_DEPENDENCIES:
                raise _refuse(
                    subject, f"it depends on more than {MAX_VIEW_DEPENDENCIES} views"
                )
            tree = self._parse(subject, _view_query(view, sql))
            for name, kind, definition in self._references(subject, tree):
                if kind == "view":
                    pending.append((root, name, definition))
                else:
                    generated[name] = None
        self._check_generated(generated)

    def _check_generated(self, generated: Mapping[str, frozenset[str] | None]) -> None:
        """Check generated columns, which DuckDB evaluates whenever they are read."""
        if not generated:
            return
        names = sorted(generated)
        placeholders = ",".join("?" for _ in names)
        scope = "database_name = current_database() AND schema_name = 'main'"
        limit = MAX_COLUMNS_PER_TABLE * len(names)
        declares_generated = {
            str(table)
            for table, sql in self._connection.execute(
                f"SELECT table_name, sql FROM duckdb_tables() WHERE {scope} "
                f"AND NOT temporary AND table_name IN ({placeholders})",
                names,
            ).fetchall()
            if "generated always" in str(sql or "").casefold()
        }
        flags = {
            (str(table), str(column)): (flag, expression)
            for table, column, flag, expression in self._connection.execute(
                "SELECT table_name, column_name, is_generated, generation_expression "
                "FROM information_schema.columns WHERE table_catalog = current_database() "
                f"AND table_schema = 'main' AND table_name IN ({placeholders})",
                names,
            ).fetchmany(limit + 1)
        }
        rows = self._connection.execute(
            "SELECT table_name, column_name, column_default FROM duckdb_columns() "
            f"WHERE {scope} AND table_name IN ({placeholders}) "
            "ORDER BY table_name, column_index",
            names,
        ).fetchmany(limit + 1)
        if len(rows) > limit or len(flags) > limit:
            raise _refuse(
                f"table {names[0]!r}", "too many columns to check generated columns"
            )
        for table, column, default in rows:
            permitted = generated.get(table)
            if permitted is not None and column not in permitted:
                continue
            flag, expression = flags.get((table, column), (None, None))
            if flag == "NEVER":
                continue
            # DuckDB before 2.0 leaves is_generated empty and reports a generation
            # expression as the column default; the table's SQL then decides.
            if flag is None:
                if table not in declares_generated or default is None:
                    continue
                expression = default
            subject = f"generated column {table}.{column}"
            query = None if expression is None else f"SELECT {expression}"
            if self._references(subject, self._parse(subject, query)):
                raise _refuse(subject, "its expression reads a table")

    def _parse(self, subject: str, query: str | None) -> Any:
        duckdb = duckdb_module()
        if query is None:
            raise _refuse(subject, "DuckDB did not report its definition in a known form")
        if len(query) > MAX_VIEW_DEFINITION_CHARS:
            raise _refuse(subject, "its definition is too large to check")
        try:
            parsed = json.loads(
                self._connection.execute(
                    "SELECT json_serialize_sql(?)", [query]
                ).fetchone()[0]
            )
        except (duckdb.Error, TypeError, ValueError):
            parsed = None
        statements = parsed.get("statements") if isinstance(parsed, dict) else None
        if (
            not isinstance(parsed, dict)
            or parsed.get("error")
            or not isinstance(statements, list)
            or len(statements) != 1
        ):
            raise _refuse(subject, "its definition could not be parsed")
        return statements[0]

    def _references(
        self, subject: str, tree: Any
    ) -> list[tuple[str, str, str | None]]:
        """Check one parsed query; return the tables and views it reads."""
        cte_names: set[str] = set()
        base_tables: list[dict[str, Any]] = []
        stack: list[Any] = [tree]
        nodes = 0
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
                continue
            if not isinstance(item, dict):
                continue
            nodes += 1
            if nodes > MAX_VIEW_SYNTAX_NODES:
                raise _refuse(subject, "its definition is too large to check")
            if item.get("sample") is not None:
                raise _refuse(subject, "it samples rows, which is not deterministic")
            expression_class = item.get("class")
            if expression_class in {"FUNCTION", "WINDOW"}:
                self._check_function(subject, item)
            elif expression_class is None:
                kind = item.get("type")
                if kind == "TABLE_FUNCTION":
                    raise _refuse(subject, "it reads from a table function")
                if kind == "BASE_TABLE":
                    base_tables.append(item)
                elif kind == "JOIN":
                    for side in (item.get("left"), item.get("right")):
                        _check_table_reference(subject, side)
                if "from_table" in item and item["from_table"] is not None:
                    _check_table_reference(subject, item["from_table"])
                cte_map = item.get("cte_map")
                if isinstance(cte_map, dict):
                    for entry in cte_map.get("map") or ():
                        if isinstance(entry, dict):
                            cte_names.add(str(entry.get("key", "")).casefold())
            stack.extend(item.values())

        references: list[tuple[str, str, str | None]] = []
        for table in base_tables:
            name = str(table.get("table_name") or "")
            catalog = str(table.get("catalog_name") or "")
            schema = str(table.get("schema_name") or "")
            if table.get("at_clause") is not None:
                raise _refuse(subject, f"it reads a past version of {name!r}")
            if (catalog and catalog.casefold() != self._database.casefold()) or (
                schema and schema.casefold() != "main"
            ):
                raise _refuse(subject, f"it reads {name!r} outside the file's main schema")
            found = self._lookup(subject, name)
            if found is None:
                if not catalog and not schema and name.casefold() in cte_names:
                    continue
                raise _refuse(
                    subject,
                    f"it reads {name!r}, which is not a table or view in the file's "
                    "main schema",
                )
            references.append(found)
        return references

    def _lookup(self, subject: str, name: str) -> tuple[str, str, str | None] | None:
        rows = self._connection.execute(
            "SELECT database_name = current_database() AND schema_name = 'main' "
            "AND NOT temporary, object_name, kind, sql FROM ("
            "SELECT database_name, schema_name, temporary, table_name AS object_name, "
            "'table' AS kind, NULL AS sql FROM duckdb_tables() UNION ALL "
            "SELECT database_name, schema_name, temporary, view_name, 'view', sql "
            "FROM duckdb_views()) WHERE lower(object_name) = lower(?) LIMIT 8",
            [name],
        ).fetchall()
        if any(not row[0] for row in rows):
            raise _refuse(
                subject,
                f"it reads {name!r}, a name DuckDB also defines outside the file's "
                "main schema",
            )
        return (str(rows[0][1]), str(rows[0][2]), rows[0][3]) if rows else None

    def _check_function(self, subject: str, node: Mapping[str, Any]) -> None:
        name = str(node.get("function_name") or "")
        if node.get("schema") or node.get("catalog"):
            raise _refuse(subject, f"it calls {name!r} through a schema")
        if node.get("class") == "WINDOW" and node.get("type") != "WINDOW_AGGREGATE":
            if node.get("type") not in _VIEW_WINDOW_TYPES:
                raise _refuse(subject, f"it calls the unsupported window function {name!r}")
            return
        key = name.lower()
        if key not in self._functions:
            duckdb = duckdb_module()
            try:
                rows = self._connection.execute(
                    "SELECT function_type, stability, internal FROM duckdb_functions() "
                    "WHERE lower(function_name) = ?",
                    [key],
                ).fetchall()
            except duckdb.Error:
                rows = []
            self._functions[key] = (
                key not in _VIEW_REFUSED_FUNCTIONS
                and bool(rows)
                and all(
                    internal is True
                    and function_type in _VIEW_FUNCTION_TYPES
                    and stability == "CONSISTENT"
                    for function_type, stability, internal in rows
                )
            )
        if not self._functions[key]:
            raise _refuse(
                subject,
                f"it calls {name!r}, which is not a built-in function DuckDB marks "
                "as consistent",
            )


def _check_table_reference(subject: str, reference: Any) -> None:
    kind = reference.get("type") if isinstance(reference, dict) else None
    if kind == "TABLE_FUNCTION":
        raise _refuse(subject, "it reads from a table function")
    if kind not in _VIEW_TABLE_REFERENCES:
        raise _refuse(subject, f"it reads from an unsupported {kind!s} source")


def _view_query(view: str, sql: str) -> str | None:
    """The query of DuckDB's ``CREATE VIEW name [(aliases)] AS query;`` text."""
    match = _CREATE_VIEW.match(sql)
    if match is None or _unquote(match["name"]) != view:
        return None
    return sql[match.end() :].rstrip().removesuffix(";")


def _unquote(identifier: str) -> str:
    if identifier.startswith('"'):
        return identifier[1:-1].replace('""', '"')
    return identifier


def _refuse(subject: str, reason: str) -> UnauthorizedDatabaseAccessError:
    return UnauthorizedDatabaseAccessError(f"{subject} is not supported: {reason}")
