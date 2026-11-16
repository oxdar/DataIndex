"""Read-only schema reflection and approved queries for SQL servers.

PostgreSQL, MySQL, MariaDB, SQL Server, Oracle, Snowflake, BigQuery and
ClickHouse are reached through SQLAlchemy Core and the engine's own driver.
Every statement is built here from an approved ``QueryDefinition`` with
SQLAlchemy expressions and bound parameters; no caller-supplied SQL text is
ever executed.

Each operation opens one fresh, unpooled connection, applies the engine's
read-only and timeout guards, and always rolls back. The guards limit damage
from mistakes; they do not replace a database login that can only read the
allowlisted tables. Use one.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from datetime import UTC, date, datetime
from datetime import time as time_of_day
from decimal import Decimal, InvalidOperation
from typing import Any

from dataindex_ext_databases.connections import ConnectionSettings, scrub
from dataindex_ext_databases.exceptions import (
    DatabaseConnectionError,
    DatabaseDeadlineExceededError,
    DatabaseIndexError,
    DatabaseQueryError,
    InvalidDatabaseToolRequestError,
    UnauthorizedDatabaseAccessError,
)
from dataindex_ext_databases.queries import (
    BoundFilter,
    ColumnRef,
    QueryDefinition,
    bind_filters,
    output_keys,
    result_ordering,
)
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

DEFAULT_TIMEOUT_SECONDS = 30.0
CONNECT_TIMEOUT_SECONDS = 10
_CONNECT_ARGS: dict[str, dict[str, object]] = {
    "postgresql+psycopg": {"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    "postgresql+psycopg2": {"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    "postgresql+pg8000": {"timeout": DEFAULT_TIMEOUT_SECONDS},
    "mysql+pymysql": {
        "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        "read_timeout": int(DEFAULT_TIMEOUT_SECONDS),
        "local_infile": False,
    },
    "mariadb+pymysql": {
        "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        "read_timeout": int(DEFAULT_TIMEOUT_SECONDS),
        "local_infile": False,
    },
    "mysql+mysqldb": {
        "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        "read_timeout": int(DEFAULT_TIMEOUT_SECONDS),
        "local_infile": 0,
    },
    "mariadb+mysqldb": {
        "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        "read_timeout": int(DEFAULT_TIMEOUT_SECONDS),
        "local_infile": 0,
    },
    "mysql+mysqlconnector": {
        "connection_timeout": CONNECT_TIMEOUT_SECONDS,
        "allow_local_infile": False,
    },
    "mariadb+mariadbconnector": {
        "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        "read_timeout": int(DEFAULT_TIMEOUT_SECONDS),
        "local_infile": False,
    },
    "mssql+pyodbc": {"timeout": CONNECT_TIMEOUT_SECONDS},
    "mssql+pymssql": {
        "login_timeout": CONNECT_TIMEOUT_SECONDS,
        "timeout": int(DEFAULT_TIMEOUT_SECONDS),
    },
    "oracle+oracledb": {"tcp_connect_timeout": CONNECT_TIMEOUT_SECONDS},
    "snowflake": {
        "login_timeout": CONNECT_TIMEOUT_SECONDS,
        "network_timeout": int(DEFAULT_TIMEOUT_SECONDS),
    },
    "clickhousedb": {
        "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        "send_receive_timeout": int(DEFAULT_TIMEOUT_SECONDS),
    },
}


def sqlalchemy_module(settings: ConnectionSettings) -> Any:
    settings.require_driver()
    import sqlalchemy

    return sqlalchemy


class SQLAlchemyServer:
    """One registered SQL server connection and its reflected column types."""

    family = "sqlalchemy"
    supports_joins = True

    def __init__(self, settings: ConnectionSettings) -> None:
        self.settings = settings
        self._types: dict[tuple[str, str], Any] = {}

    def inspect(
        self,
        allowed_columns: Mapping[str, frozenset[str]],
        deadline_monotonic: float | None,
    ) -> SchemaInfo:
        """Reflect the allowlisted tables and views; ``version`` is their digest."""
        sa = sqlalchemy_module(self.settings)
        with self._session("read the schema of", deadline_monotonic) as (connection, _):
            schema, types = reflect_schema(
                sa, connection, self.settings.schema, allowed_columns
            )
        self._types = types
        return schema

    def run(
        self,
        query: QueryDefinition,
        filters: Mapping[str, object],
        limit: int,
        deadline_monotonic: float | None,
    ) -> tuple[list[dict[str, JsonScalar]], bool, str]:
        """Run one approved query; returns rows, truncation and the read time."""
        bound = bind_filters(query, filters, limit)
        sa = sqlalchemy_module(self.settings)
        statement = build_statement(
            sa, query, bound, limit, self._types, self.settings.schema
        )
        label = self.settings.engine.label
        with self._session("query", deadline_monotonic) as (connection, password):
            read_timestamp = utc_timestamp()
            timeout = _timeout(deadline_monotonic)
            started = time.monotonic()
            try:
                records = connection.execute(statement).fetchmany(limit + 1)
            except sa.exc.SQLAlchemyError as error:
                # Drivers report statement timeouts with engine-specific errors;
                # an error raised at the guard's timeout is treated as one.
                if time.monotonic() - started >= timeout - 0.05:
                    raise DatabaseDeadlineExceededError(
                        f"{label} query exceeded its deadline"
                    ) from None
                raise DatabaseQueryError(
                    f"approved {label} query failed: {scrub(error, password)}"
                ) from None
            rows, truncated = collect_rows(
                [tuple(record) for record in records], output_keys(query), limit
            )
        _check_deadline(deadline_monotonic)
        return rows, truncated, read_timestamp

    @contextmanager
    def _session(
        self, action: str, deadline_monotonic: float | None
    ) -> Iterator[tuple[Any, str | None]]:
        sa = sqlalchemy_module(self.settings)
        timeout = _timeout(deadline_monotonic)
        password = self.settings.password()
        engine = None
        try:
            try:
                url = sa.engine.make_url(self.settings.url)
                if password is not None:
                    url = url.set(password=password)
                engine = sa.create_engine(
                    url,
                    poolclass=sa.pool.NullPool,
                    connect_args=dict(_CONNECT_ARGS.get(self.settings.scheme, {})),
                    hide_parameters=True,
                )
                connection = engine.connect()
            except DatabaseIndexError:
                raise
            except Exception as error:
                raise self.settings.connection_error("connect to", error, password) from None
            with connection:
                try:
                    apply_read_only_guards(self.settings.engine.name, connection, timeout)
                except DatabaseIndexError:
                    raise
                except Exception as error:
                    raise self.settings.connection_error(
                        "start a read-only session on", error, password
                    ) from None
                try:
                    yield connection, password
                except DatabaseIndexError:
                    raise
                except Exception as error:
                    raise self.settings.connection_error(action, error, password) from None
                finally:
                    _rollback(connection)
        finally:
            if engine is not None:
                engine.dispose()


def apply_read_only_guards(engine: str, connection: Any, timeout_seconds: float) -> None:
    """Start a read-only transaction with statement and lock timeouts.

    Each engine gets the strongest session-level guard it offers. SQL Server,
    Snowflake, BigQuery and ClickHouse have no read-only transaction mode, so
    they depend on a read-only login; every session is still rolled back.
    """
    milliseconds = max(1, int(timeout_seconds * 1000))
    seconds = max(1, math.ceil(timeout_seconds))
    raw = connection.connection.dbapi_connection
    if engine == "postgresql":
        raw.rollback()
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        connection.exec_driver_sql(
            f"SELECT set_config('statement_timeout', '{milliseconds:d}', true), "
            f"set_config('lock_timeout', '{milliseconds:d}', true)"
        )
        state = connection.exec_driver_sql(
            "SELECT current_setting('transaction_read_only')"
        ).scalar()
        _require_read_only(state == "on")
    elif engine in {"mysql", "mariadb"}:
        raw.rollback()
        connection.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
        connection.rollback()
        if engine == "mariadb" or getattr(connection.dialect, "is_mariadb", False):
            connection.exec_driver_sql(
                f"SET SESSION max_statement_time = {seconds:d}"
            )
            # MariaDB 11.1 renamed tx_read_only to transaction_read_only.
            version = tuple(getattr(connection.dialect, "server_version_info", None) or ())
            variable = (
                "@@session.transaction_read_only"
                if version[:2] >= (11, 1)
                else "@@session.tx_read_only"
            )
        else:
            connection.exec_driver_sql(
                f"SET SESSION max_execution_time = {milliseconds:d}"
            )
            variable = "@@session.transaction_read_only"
        state = connection.exec_driver_sql(f"SELECT {variable}").scalar()
        _require_read_only(str(state) == "1")
    elif engine == "mssql":
        connection.exec_driver_sql(f"SET LOCK_TIMEOUT {milliseconds:d}")
        if hasattr(raw, "timeout"):
            raw.timeout = seconds
    elif engine == "oracle":
        raw.rollback()
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        if hasattr(raw, "call_timeout"):
            raw.call_timeout = milliseconds
    elif engine == "snowflake":
        connection.exec_driver_sql(
            f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {seconds:d}"
        )
    elif engine == "clickhouse":
        client = getattr(raw, "client", None)
        set_setting = getattr(client, "set_client_setting", None)
        if callable(set_setting):
            set_setting("max_execution_time", str(seconds))


def reflect_schema(
    sa: Any,
    connection: Any,
    db_schema: str | None,
    allowed_columns: Mapping[str, frozenset[str]],
) -> tuple[SchemaInfo, dict[tuple[str, str], Any]]:
    from sqlalchemy.engine.reflection import ObjectKind

    inspector = sa.inspect(connection)
    names = sorted(allowed_columns)
    options = {
        "schema": db_schema,
        "filter_names": names,
        "kind": ObjectKind.ANY,
    }
    columns = inspector.get_multi_columns(**options)
    primary_keys = inspector.get_multi_pk_constraint(**options)
    foreign_keys = inspector.get_multi_foreign_keys(**options)
    views = _names(inspector.get_view_names, db_schema) | _names(
        inspector.get_materialized_view_names, db_schema
    )
    dialect = connection.dialect

    missing_tables = sorted(
        name for name in names if (db_schema, name) not in columns
    )
    if missing_tables:
        raise UnauthorizedDatabaseAccessError(
            f"allowlisted tables or views are absent: {missing_tables}"
        )
    digest_source: dict[str, object] = {}
    tables: dict[str, TableInfo] = {}
    types: dict[tuple[str, str], Any] = {}
    foreign_key_count = 0
    for name in names:
        permitted = allowed_columns[name]
        reflected = list(columns[(db_schema, name)])
        if len(reflected) > MAX_COLUMNS_PER_TABLE:
            raise UnauthorizedDatabaseAccessError(
                f"table {name!r} exceeds the supported schema-column limit"
            )
        primary_key = tuple(
            (primary_keys.get((db_schema, name)) or {}).get("constrained_columns")
            or ()
        )
        available: dict[str, ColumnInfo] = {}
        described: list[list[object]] = []
        for ordinal, column in enumerate(reflected):
            column_name = column["name"]
            declared_type = _type_name(column["type"], dialect)
            nullable = bool(column.get("nullable", True))
            described.append([column_name, declared_type, nullable])
            available[column_name] = ColumnInfo(
                name=column_name,
                declared_type=declared_type,
                not_null=not nullable,
                primary_key_position=(
                    primary_key.index(column_name) + 1
                    if column_name in primary_key
                    else 0
                ),
                ordinal=ordinal,
            )
            if column_name in permitted:
                types[(name, column_name)] = column["type"]
        missing_columns = sorted(permitted - set(available))
        if missing_columns:
            raise UnauthorizedDatabaseAccessError(
                f"allowlisted columns are absent from {name!r}: {missing_columns}"
            )
        table_foreign_keys: list[ForeignKeyInfo] = []
        described_keys: list[list[object]] = []
        for constraint_id, foreign_key in enumerate(
            foreign_keys.get((db_schema, name)) or ()
        ):
            foreign_key_count += 1
            if foreign_key_count > MAX_SCHEMA_FOREIGN_KEYS:
                raise UnauthorizedDatabaseAccessError(
                    f"allowlisted schema exceeds the {MAX_SCHEMA_FOREIGN_KEYS}-"
                    "foreign-key metadata limit"
                )
            from_columns = list(foreign_key.get("constrained_columns") or ())
            to_columns = list(foreign_key.get("referred_columns") or ())
            target = foreign_key.get("referred_table")
            referred_schema = foreign_key.get("referred_schema")
            described_keys.append([from_columns, referred_schema, target, to_columns])
            if (
                referred_schema not in (None, db_schema)
                or target not in allowed_columns
                or not from_columns
                or len(from_columns) != len(to_columns)
                or any(column not in permitted for column in from_columns)
                or any(column not in allowed_columns[target] for column in to_columns)
            ):
                continue
            option_values = foreign_key.get("options") or {}
            table_foreign_keys.extend(
                ForeignKeyInfo(
                    constraint_id=constraint_id,
                    sequence=sequence,
                    from_table=name,
                    from_column=from_column,
                    to_table=target,
                    to_column=to_column,
                    on_update=str(option_values.get("onupdate") or "NO ACTION").upper(),
                    on_delete=str(option_values.get("ondelete") or "NO ACTION").upper(),
                )
                for sequence, (from_column, to_column) in enumerate(
                    zip(from_columns, to_columns, strict=True)
                )
            )
        tables[name] = TableInfo(
            name=name,
            columns=tuple(
                available[column["name"]]
                for column in reflected
                if column["name"] in permitted
            ),
            foreign_keys=tuple(table_foreign_keys),
            primary_key_columns=primary_key,
            object_kind="view" if name in views else "table",
        )
        digest_source[name] = {
            "kind": "view" if name in views else "table",
            "columns": described,
            "primary_key": list(primary_key),
            "foreign_keys": described_keys,
        }
    digest = hashlib.sha256(
        json.dumps(
            digest_source, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
    ).hexdigest()
    return SchemaInfo(digest, tables), types


def build_statement(
    sa: Any,
    query: QueryDefinition,
    bound: tuple[BoundFilter, ...],
    limit: int,
    types: Mapping[tuple[str, str], Any],
    db_schema: str | None,
) -> Any:
    """Compile an approved definition into a SQLAlchemy Core ``Select``.

    Output columns are labelled ``c0``, ``c1``... and ``a0`` so every engine's
    identifier rules accept them; rows are read by position.
    """
    references: dict[str, list[str]] = {}

    def remember(column: ColumnRef) -> None:
        names = references.setdefault(column.table, [])
        if column.column not in names:
            names.append(column.column)

    remember_all = [
        *query.columns,
        *query.group_by,
        *query.order_by,
        *(item.column for item in bound),
        *(endpoint for join in query.joins for endpoint in (join.left, join.right)),
    ]
    if query.aggregate is not None and query.aggregate.column is not None:
        remember_all.append(query.aggregate.column)
    references.setdefault(query.table, [])
    for column in remember_all:
        remember(column)
    tables = {
        table: sa.table(
            table,
            *(
                sa.column(name, types.get((table, name)))
                for name in names
            ),
            schema=db_schema,
        )
        for table, names in references.items()
    }

    def expression(column: ColumnRef) -> Any:
        return tables[column.table].c[column.column]

    selected = [
        expression(column).label(f"c{index}")
        for index, column in enumerate(query.columns)
    ]
    if query.aggregate is not None:
        aggregate = query.aggregate
        if aggregate.function == "count":
            value = (
                sa.func.count()
                if aggregate.column is None
                else sa.func.count(expression(aggregate.column))
            )
        else:
            assert aggregate.column is not None
            value = sa.func.sum(expression(aggregate.column))
        selected.append(value.label("a0"))
    source = tables[query.table]
    for join in query.joins:
        source = source.join(
            tables[join.right.table],
            expression(join.left) == expression(join.right),
            isouter=join.kind == "left",
        )
    statement = sa.select(*selected).select_from(source)
    for item in bound:
        column = expression(item.column)
        column_type = types.get((item.column.table, item.column.column))
        if item.operator == "is_null":
            statement = statement.where(column.is_(None))
        elif item.operator == "is_not_null":
            statement = statement.where(column.is_not(None))
        elif item.operator == "in":
            statement = statement.where(
                column.in_([coerce_filter_value(sa, value, column_type) for value in item.values])
            )
        else:
            value = coerce_filter_value(sa, item.values[0], column_type)
            statement = statement.where(_compare(column, item.operator, value))
    if query.aggregate is not None and query.group_by:
        statement = statement.group_by(*(expression(column) for column in query.group_by))
    ordering = result_ordering(query)
    if ordering:
        statement = statement.order_by(*(expression(column) for column in ordering))
    return statement.limit(limit + 1)


def coerce_filter_value(sa: Any, value: object, column_type: Any) -> object:
    """Turn ISO text into the date, time, UUID or decimal a typed column expects.

    Drivers such as psycopg bind parameters by their Python type, so a date
    column compared with a string would fail or compare as text.
    """
    if not isinstance(value, str) or column_type is None:
        return value
    affinity = getattr(column_type, "_type_affinity", None)
    if not isinstance(affinity, type):
        return value
    try:
        if issubclass(affinity, sa.DateTime):
            return datetime.fromisoformat(value)
        if issubclass(affinity, sa.Date):
            return date.fromisoformat(value)
        if issubclass(affinity, sa.Time):
            return time_of_day.fromisoformat(value)
        if issubclass(affinity, sa.Uuid):
            return uuid.UUID(value)
        if issubclass(affinity, sa.Numeric) and not issubclass(affinity, sa.Float):
            number = Decimal(value)
            if not number.is_finite():
                raise ValueError(value)
            return number
    except (ValueError, InvalidOperation):
        raise InvalidDatabaseToolRequestError(
            f"filter value {value[:64]!r} does not match its column type"
        ) from None
    return value


def utc_timestamp() -> str:
    return (
        datetime.now(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _compare(column: Any, operator: str, value: object) -> Any:
    if operator == "eq":
        return column == value
    if operator == "ne":
        return column != value
    if operator == "lt":
        return column < value
    if operator == "lte":
        return column <= value
    if operator == "gt":
        return column > value
    return column >= value


def _names(listing: Any, db_schema: str | None) -> set[str]:
    try:
        return set(listing(schema=db_schema))
    except NotImplementedError:
        return set()


def _type_name(column_type: Any, dialect: Any) -> str:
    try:
        name = str(column_type.compile(dialect=dialect))
    except Exception:
        name = type(column_type).__name__
    return name[:MAX_DECLARED_TYPE_CHARS]


def _require_read_only(confirmed: bool) -> None:
    if not confirmed:
        raise DatabaseConnectionError(
            "the server did not confirm a read-only transaction"
        )


def _rollback(connection: Any) -> None:
    with suppress(Exception):
        connection.rollback()


def _timeout(deadline_monotonic: float | None) -> float:
    if deadline_monotonic is None:
        return DEFAULT_TIMEOUT_SECONDS
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        raise DatabaseDeadlineExceededError(
            "database operation exceeded its cooperative deadline"
        )
    return min(remaining, DEFAULT_TIMEOUT_SECONDS)


def _check_deadline(deadline_monotonic: float | None) -> None:
    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
        raise DatabaseDeadlineExceededError(
            "database operation exceeded its cooperative deadline"
        )
