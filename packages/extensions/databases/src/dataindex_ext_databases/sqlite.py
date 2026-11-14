"""Read-only SQLite introspection and bounded query execution."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dataindex_ext_databases.exceptions import (
    DatabaseDeadlineExceededError,
    DatabaseExecutionBudgetExceededError,
    DatabaseQueryError,
    DatabaseResultError,
    DatabaseResultTooLargeError,
    UnauthorizedDatabaseAccessError,
)
from dataindex_ext_databases.queries import (
    QueryDefinition,
    build_query,
    quote_identifier,
)

MAX_SQLITE_VM_INSTRUCTIONS = 100_000
SQLITE_PROGRESS_INTERVAL = 1_000
SQLITE_BUSY_TIMEOUT_SECONDS = 1.0
MAX_SQLITE_VALUE_BYTES = 64 * 1024
MAX_SQLITE_RESULT_BYTES = 64 * 1024
MAX_SQLITE_RESULT_ROWS = 100
MAX_SCHEMA_TABLES = 256
MAX_SCHEMA_COLUMNS = 4096
MAX_COLUMNS_PER_TABLE = 512
MAX_SCHEMA_FOREIGN_KEYS = 4096


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    name: str
    declared_type: str
    not_null: bool
    primary_key_position: int
    ordinal: int


@dataclass(frozen=True, slots=True)
class ForeignKeyInfo:
    constraint_id: int
    sequence: int
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    on_update: str
    on_delete: str


@dataclass(frozen=True, slots=True)
class TableInfo:
    name: str
    columns: tuple[ColumnInfo, ...]
    foreign_keys: tuple[ForeignKeyInfo, ...]
    primary_key_columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SchemaInfo:
    version: int
    tables: Mapping[str, TableInfo]


def open_read_only(path: Path) -> sqlite3.Connection:
    """Open one explicitly supplied file without granting SQLite write access."""
    uri = f"{path.as_uri()}?mode=ro"
    connection = sqlite3.connect(
        uri,
        uri=True,
        timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
        isolation_level=None,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_SQLITE_VALUE_BYTES)
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
    except sqlite3.Error:
        connection.close()
        raise
    return connection


def inspect_schema(
    connection: sqlite3.Connection,
    allowed_columns: Mapping[str, frozenset[str]],
) -> SchemaInfo:
    schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
    placeholders = ",".join("?" for _ in allowed_columns)
    database_tables = {
        row["name"]: row["sql"]
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_schema "
            f"WHERE type = 'table' AND name IN ({placeholders}) ORDER BY name",
            tuple(allowed_columns),
        )
    }
    missing_tables = sorted(set(allowed_columns) - set(database_tables))
    if missing_tables:
        raise UnauthorizedDatabaseAccessError(
            f"allowlisted tables are absent: {missing_tables}"
        )

    table_columns: dict[str, tuple[ColumnInfo, ...]] = {}
    table_primary_keys: dict[str, tuple[str, ...]] = {}
    for table_name, permitted_columns in allowed_columns.items():
        create_sql = database_tables[table_name] or ""
        if re.match(r"\s*CREATE\s+VIRTUAL\s+TABLE\b", create_sql, re.IGNORECASE):
            raise UnauthorizedDatabaseAccessError(
                f"virtual table {table_name!r} is not supported"
            )
        rows = connection.execute(
            f"PRAGMA table_info({quote_identifier(table_name)})"
        ).fetchmany(MAX_COLUMNS_PER_TABLE + 1)
        if len(rows) > MAX_COLUMNS_PER_TABLE:
            raise UnauthorizedDatabaseAccessError(
                f"table {table_name!r} exceeds the supported schema-column limit"
            )
        available = {
            row["name"]: ColumnInfo(
                name=row["name"],
                declared_type=row["type"] or "",
                not_null=bool(row["notnull"]),
                primary_key_position=int(row["pk"]),
                ordinal=int(row["cid"]),
            )
            for row in rows
        }
        missing_columns = sorted(permitted_columns - set(available))
        if missing_columns:
            raise UnauthorizedDatabaseAccessError(
                f"allowlisted columns are absent from {table_name!r}: "
                f"{missing_columns}"
            )
        table_primary_keys[table_name] = tuple(
            column.name
            for column in sorted(
                available.values(), key=lambda item: item.primary_key_position
            )
            if column.primary_key_position
        )
        table_columns[table_name] = tuple(
            available[column_name]
            for column_name in available
            if column_name in permitted_columns
        )
    if sum(len(columns) for columns in table_columns.values()) > MAX_SCHEMA_COLUMNS:
        raise UnauthorizedDatabaseAccessError(
            f"allowlist exceeds the {MAX_SCHEMA_COLUMNS}-column schema limit"
        )

    table_info: dict[str, TableInfo] = {}
    foreign_key_count = 0
    for table_name, columns in table_columns.items():
        foreign_keys: list[ForeignKeyInfo] = []
        remaining_foreign_keys = MAX_SCHEMA_FOREIGN_KEYS - foreign_key_count
        rows = connection.execute(
            f"PRAGMA foreign_key_list({quote_identifier(table_name)})"
        ).fetchmany(remaining_foreign_keys + 1)
        if len(rows) > remaining_foreign_keys:
            raise UnauthorizedDatabaseAccessError(
                f"allowlisted schema exceeds the {MAX_SCHEMA_FOREIGN_KEYS}-foreign-key "
                "metadata limit"
            )
        foreign_key_count += len(rows)
        grouped_rows: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            grouped_rows.setdefault(int(row["id"]), []).append(row)
        for constraint_id, constraint_rows in grouped_rows.items():
            target_table = constraint_rows[0]["table"]
            if target_table not in allowed_columns:
                continue
            target_keys = table_primary_keys[target_table]
            constraint_rows.sort(key=lambda row: int(row["seq"]))
            resolved_columns: list[tuple[sqlite3.Row, str]] = []
            for row in constraint_rows:
                to_column = row["to"]
                if to_column is None:
                    sequence = int(row["seq"])
                    if len(target_keys) != len(constraint_rows) or sequence >= len(
                        target_keys
                    ):
                        resolved_columns = []
                        break
                    to_column = target_keys[sequence]
                resolved_columns.append((row, to_column))
            if not resolved_columns or any(
                row["from"] not in allowed_columns[table_name]
                or to_column not in allowed_columns[target_table]
                for row, to_column in resolved_columns
            ):
                continue
            foreign_keys.extend(
                ForeignKeyInfo(
                    constraint_id=constraint_id,
                    sequence=int(row["seq"]),
                    from_table=table_name,
                    from_column=row["from"],
                    to_table=target_table,
                    to_column=to_column,
                    on_update=row["on_update"],
                    on_delete=row["on_delete"],
                )
                for row, to_column in resolved_columns
            )
        table_info[table_name] = TableInfo(
            name=table_name,
            columns=columns,
            foreign_keys=tuple(foreign_keys),
            primary_key_columns=table_primary_keys[table_name],
        )
    return SchemaInfo(schema_version, table_info)


def query_schema_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA schema_version").fetchone()[0])


def run_bounded_query(
    connection: sqlite3.Connection,
    definition: QueryDefinition,
    filters: Mapping[str, object],
    limit: int,
    allowed_columns: Mapping[str, frozenset[str]],
    deadline_monotonic: float | None,
) -> tuple[list[dict[str, object]], bool]:
    statement, parameters = build_query(definition, filters, limit)
    allowed_tables = frozenset(allowed_columns)
    allowed_fields = frozenset(
        (table, column)
        for table, columns in allowed_columns.items()
        for column in columns
    )

    def authorize(
        action: int,
        first: str | None,
        second: str | None,
        database: str | None,
        trigger: str | None,
    ) -> int:
        del trigger
        if action in {
            sqlite3.SQLITE_SELECT,
            getattr(sqlite3, "SQLITE_RECURSIVE", -100),
        }:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            if first in allowed_tables and second == "" and database in {None, "main"}:
                # SQLite reports a table read that needs no ordinary column (COUNT(*),
                # a rowid-only selection, a key-only join) as an empty column name.
                return sqlite3.SQLITE_OK
            if database != "main" or first not in allowed_tables:
                return sqlite3.SQLITE_DENY
            if second is None or (first, second) in allowed_fields:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            function_name = (second or first or "").upper()
            return (
                sqlite3.SQLITE_OK
                if function_name in {"COUNT", "SUM"}
                else sqlite3.SQLITE_DENY
            )
        return sqlite3.SQLITE_DENY

    progress_reason: str | None = None
    instructions = 0

    def progress() -> int:
        nonlocal progress_reason, instructions
        if (
            deadline_monotonic is not None
            and time.monotonic() >= deadline_monotonic
        ):
            progress_reason = "deadline"
            return 1
        instructions += SQLITE_PROGRESS_INTERVAL
        if instructions >= MAX_SQLITE_VM_INSTRUCTIONS:
            progress_reason = "budget"
            return 1
        return 0

    connection.set_authorizer(authorize)
    connection.set_progress_handler(progress, SQLITE_PROGRESS_INTERVAL)
    try:
        cursor = connection.execute(statement, parameters)
        column_names = tuple(column[0] for column in cursor.description or ())
        truncated = False
        rows: list[dict[str, object]] = []
        result_size = 2
        for record_index, record in enumerate(cursor):
            if record_index == limit:
                truncated = True
                break
            row: dict[str, object] = {}
            row_size = 2
            for column_index, key in enumerate(column_names):
                value = record[column_index]
                if isinstance(value, bytes):
                    raise DatabaseResultError(
                        "binary SQLite values cannot be returned as query evidence"
                    )
                if isinstance(value, float) and not math.isfinite(value):
                    raise DatabaseResultError(
                        "non-finite SQLite values cannot be returned as query evidence"
                    )
                if value is not None and not isinstance(value, (str, int, float)):
                    raise DatabaseResultError(
                        "SQLite returned a value that is not JSON-compatible"
                    )
                row[key] = value
                try:
                    key_size = len(
                        json.dumps(
                            key, ensure_ascii=False, separators=(",", ":")
                        ).encode("utf-8")
                    )
                    value_size = len(
                        json.dumps(
                            value,
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    )
                except (TypeError, ValueError) as error:
                    raise DatabaseResultError(
                        "SQLite result could not be represented as JSON evidence"
                    ) from error
                if len(row) > 1:
                    row_size += 1
                row_size += key_size + 1 + value_size
                if row_size > MAX_SQLITE_RESULT_BYTES:
                    raise DatabaseResultTooLargeError(
                        f"query row exceeds the {MAX_SQLITE_RESULT_BYTES}-byte limit"
                    )
            if rows:
                result_size += 1
            result_size += row_size
            if result_size > MAX_SQLITE_RESULT_BYTES:
                raise DatabaseResultTooLargeError(
                    f"query result exceeds the {MAX_SQLITE_RESULT_BYTES}-byte limit"
                )
            rows.append(row)
    except sqlite3.Error as error:
        if progress_reason == "deadline":
            raise DatabaseDeadlineExceededError(
                "SQLite query exceeded its deadline"
            ) from error
        if progress_reason == "budget":
            raise DatabaseExecutionBudgetExceededError(
                "SQLite query exceeded its virtual-machine instruction budget"
            ) from error
        if "not authorized" in str(error).casefold():
            raise UnauthorizedDatabaseAccessError(
                "SQLite denied a query operation outside the allowlist"
            ) from error
        raise DatabaseQueryError("approved SQLite query failed") from error
    finally:
        connection.set_progress_handler(None, 0)
        connection.set_authorizer(None)
    return rows, truncated
