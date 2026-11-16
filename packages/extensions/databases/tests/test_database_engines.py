"""One contract scenario for every database engine.

DuckDB runs in-process. The SQLAlchemy server path runs against SQLite through
a test-only engine entry, and MongoDB runs against ``mongomock``. Real servers
are exercised when ``DATAINDEX_TEST_<ENGINE>_URL`` holds a password-free URL
and ``DATAINDEX_TEST_<ENGINE>_PASSWORD`` holds its password.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dataindex.models import (
    EvidenceKind,
    EvidenceRecord,
    IndexContext,
    RetrievalContext,
    SourceRef,
    ToolRequest,
)
from dataindex_ext_databases import (
    AggregateRule,
    ColumnRef,
    DatabaseConnectionError,
    DatabaseExtension,
    DatabaseRegistrationError,
    DatabaseSchemaChangedError,
    DatabaseSourceChangedError,
    FilterRule,
    FixedFilterRule,
    InvalidDatabaseConfigurationError,
    InvalidDatabaseToolRequestError,
    JoinRule,
    QueryDefinition,
    parse_connection,
)
from dataindex_ext_databases import connections as connections_module
from dataindex_ext_databases import duckdb_backend, mongodb_backend
from dataindex_ext_databases import plugin as plugin_module
from dataindex_ext_databases.connections import ConnectionSettings, EngineSpec
from dataindex_ext_databases.mongodb_backend import build_match, coerce_value
from dataindex_ext_databases.plugin import QUERY_TOOL_ID
from dataindex_ext_databases.queries import BoundFilter, bind_filters
from dataindex_ext_databases.sqlalchemy_backend import (
    SQLAlchemyServer,
    apply_read_only_guards,
    build_statement,
)
from test_databases import (
    ALLOWED_COLUMNS,
    OPEN_TENANT_A,
    create_database,
    query_definitions,
)

FILTERS = {"due_before": "2026-02-28", "is_paid": False}
CONTRACTS = (
    (1, "Northwind Services", "USD", "tenant-a"),
    (2, "Fabrikam Operations", "USD", "tenant-a"),
    (3, "Contoso Internal", "USD", "tenant-b"),
)
INVOICES = (
    ("INV-1001", 1, 125000, date(2026, 1, 15), False, "tenant-a", "NW-2026-01"),
    ("INV-1002", 1, 25000, date(2026, 2, 1), False, "tenant-a", "NW-2026-02"),
    ("INV-2001", 2, 90000, date(2026, 2, 10), False, "tenant-a", "FK-2026-02"),
    ("INV-2002", 2, 50000, date(2026, 2, 20), True, "tenant-a", "FK-2026-01"),
    ("INV-1003", 1, 70000, date(2026, 3, 15), False, "tenant-a", "NW-2026-03"),
    ("INV-9001", 3, 9000000, date(2026, 1, 1), False, "tenant-b", "CT-2026-01"),
)
# integer, text, big integer, date, boolean
COLUMN_TYPES = {
    "duckdb": ("INTEGER", "VARCHAR", "BIGINT", "DATE", "BOOLEAN"),
    "postgresql": ("INTEGER", "VARCHAR(64)", "BIGINT", "DATE", "BOOLEAN"),
    "mysql": ("INTEGER", "VARCHAR(64)", "BIGINT", "DATE", "BOOLEAN"),
    "mariadb": ("INTEGER", "VARCHAR(64)", "BIGINT", "DATE", "BOOLEAN"),
    "mssql": ("INT", "NVARCHAR(64)", "BIGINT", "DATE", "BIT"),
    "oracle": ("NUMBER(10)", "VARCHAR2(64)", "NUMBER(19)", "DATE", "BOOLEAN"),
}
LIVE_SQL_ENGINES = ("postgresql", "mysql", "mariadb", "mssql", "oracle")
READ_ONLY_TRANSACTION_ENGINES = {"postgresql", "mysql", "mariadb", "oracle"}

TEST_ENGINE = "sqlitetest"
TEST_SOURCE_TYPE = f"application/vnd.dataindex.db.{TEST_ENGINE}"
TEST_CONNECTION = {"url": "sqlite+pysqlite:///server.db"}

VIEW = "open_invoices"
VIEW_COLUMNS = {VIEW: ("invoice_id", "contract_id", "amount_cents", "tenant_id")}
OPEN_PIPELINE = [
    {"$match": {"paid": False}},
    {
        "$project": {
            "_id": 0,
            "invoice_id": "$_id",
            "contract_id": 1,
            "amount_cents": 1,
            "tenant_id": 1,
        }
    },
]

MONGO_SECRET = "m0ng0-s3cret"
MONGO_PASSWORD_ENV = "DATAINDEX_TEST_MONGO_PW"
MONGO_ALLOWED = {
    "invoices": (
        "_id",
        "contract_id",
        "amount_cents",
        "due_date",
        "paid",
        "tenant_id",
        "reference",
        "customer.country",
    )
}
MONGO_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": [
            "contract_id",
            "amount_cents",
            "due_date",
            "paid",
            "tenant_id",
            "reference",
        ],
        "properties": {
            "contract_id": {"bsonType": "int"},
            "amount_cents": {"bsonType": ["int", "long"]},
            "due_date": {"bsonType": "string"},
            "paid": {"bsonType": "bool"},
            "tenant_id": {"bsonType": "string"},
            "reference": {"bsonType": "string"},
            "customer": {
                "bsonType": "object",
                "properties": {"country": {"bsonType": "string"}},
            },
        },
    }
}
COUNTRIES = {
    "INV-1001": "US",
    "INV-1002": "US",
    "INV-2001": "DE",
    "INV-2002": "DE",
    "INV-1003": "US",
    "INV-9001": "DE",
}


def ask(
    extension: DatabaseExtension,
    source: SourceRef,
    query_id: str,
    filters: Mapping[str, object],
    **parameters: object,
) -> EvidenceRecord:
    return extension.run_tool(
        ToolRequest(
            QUERY_TOOL_ID,
            {
                "source_id": source.source_id,
                "source_version": source.version,
                "query_id": query_id,
                "filters": dict(filters),
                **parameters,
            },
        ),
        RetrievalContext(),
    )


def connection_config(
    connection: Mapping[str, str],
    allowed: Mapping[str, Sequence[str]] = ALLOWED_COLUMNS,
    queries: Sequence[QueryDefinition] | None = None,
) -> dict[str, object]:
    return {
        "connection": dict(connection),
        "allowed_columns": {table: list(columns) for table, columns in allowed.items()},
        "queries": [
            query.to_mapping() for query in (queries or query_definitions())
        ],
    }


def assert_relational_contract(
    extension: DatabaseExtension, source: SourceRef, engine: str
) -> None:
    nodes = list(extension.iter_index_records(source, IndexContext()))
    root = nodes[0]
    assert root.kind == "database"
    assert root.locator.scheme == f"{engine}-schema"
    assert root.attributes["engine"] == engine
    tables = {node.title: node for node in nodes if node.kind == "database_table"}
    assert set(tables) == {"contracts", "invoices"}
    assert {node.attributes["object_kind"] for node in tables.values()} == {"table"}
    assert tables["invoices"].attributes["primary_key_columns"] == ["invoice_id"]
    foreign_keys = [
        node.attributes for node in nodes if node.kind == "database_foreign_key"
    ]
    assert [
        (key["from_table"], key["from_column"], key["to_table"], key["to_column"])
        for key in foreign_keys
    ] == [("invoices", "contract_id", "contracts", "contract_id")]
    assert foreign_keys[0]["target_node_id"] == tables["contracts"].node_id
    assert "INV-1001" not in json.dumps([node.attributes for node in nodes])

    overdue = ask(extension, source, "invoices.overdue", FILTERS)
    assert overdue.evidence_kind is EvidenceKind.OBSERVED
    assert overdue.locator.scheme == f"{engine}-query"
    assert overdue.locator.value["engine"] == engine
    assert overdue.value["read_timestamp_utc"].endswith("Z")
    assert [row["invoices__invoice_id"] for row in overdue.value["rows"]] == [
        "INV-1001",
        "INV-1002",
        "INV-2001",
    ]
    first = overdue.value["rows"][0]
    assert first["invoices__contract_id"] == 1
    assert first["invoices__amount_cents"] == 125000
    assert str(first["invoices__due_date"]).startswith("2026-01-15")

    totals = ask(extension, source, "invoices.overdue_total_by_contract", FILTERS)
    assert totals.evidence_kind is EvidenceKind.CALCULATED
    assert totals.unit == "USD cents"
    assert totals.value["rows"] == [
        {
            "contracts__contract_id": 1,
            "contracts__contract_name": "Northwind Services",
            "overdue_total_cents": 150000,
        },
        {
            "contracts__contract_id": 2,
            "contracts__contract_name": "Fabrikam Operations",
            "overdue_total_cents": 90000,
        },
    ]
    count = ask(extension, source, "invoices.overdue_count", FILTERS)
    assert count.value["rows"] == [{"invoice_count": 3}]
    for reference in ("' OR 1=1 --", "CT-2026-01"):
        found = ask(extension, source, "invoices.by_reference", {"reference": reference})
        assert found.value["rows"] == []
    limited = ask(extension, source, "invoices.overdue", FILTERS, limit=2)
    assert limited.value["row_count"] == 2
    assert limited.value["truncated"] is True


def fixture_statements(engine: str) -> tuple[str, ...]:
    integer, text, big_integer, day, flag = COLUMN_TYPES[engine]
    return (
        f"DROP VIEW IF EXISTS {VIEW}",
        "DROP TABLE IF EXISTS invoices",
        "DROP TABLE IF EXISTS contracts",
        (
            f"CREATE TABLE contracts (contract_id {integer} NOT NULL PRIMARY KEY, "
            f"contract_name {text} NOT NULL, currency {text} NOT NULL, "
            f"tenant_id {text} NOT NULL)"
        ),
        (
            f"CREATE TABLE invoices (invoice_id {text} NOT NULL PRIMARY KEY, "
            f"contract_id {integer} NOT NULL, amount_cents {big_integer} NOT NULL, "
            f"due_date {day} NOT NULL, paid {flag} NOT NULL, tenant_id {text} NOT NULL, "
            f"reference {text} NOT NULL, "
            "FOREIGN KEY (contract_id) REFERENCES contracts (contract_id))"
        ),
        view_statement(engine),
    )


def view_statement(engine: str) -> str:
    unpaid = "0" if engine == "mssql" else "FALSE"
    return (
        f"CREATE VIEW {VIEW} AS SELECT invoice_id, contract_id, amount_cents, tenant_id "
        f"FROM invoices WHERE paid = {unpaid}"
    )


def view_queries() -> tuple[QueryDefinition, ...]:
    """A keyless view: rows are ordered by every selected column."""

    def column(name: str) -> ColumnRef:
        return ColumnRef(VIEW, name)

    selected = (column("contract_id"), column("invoice_id"), column("amount_cents"))
    tenant = (FixedFilterRule("tenant", column("tenant_id"), "eq", "tenant-a"),)
    return (
        QueryDefinition(
            "open.rows",
            VIEW,
            selected,
            source_filters=tenant,
            order_by=selected,
            max_rows=10,
        ),
        QueryDefinition(
            "open.totals",
            VIEW,
            (column("contract_id"),),
            source_filters=tenant,
            aggregate=AggregateRule("sum", column("amount_cents"), "open_cents"),
            group_by=(column("contract_id"),),
            max_rows=10,
        ),
    )


def assert_view_contract(extension: DatabaseExtension, source: SourceRef) -> None:
    nodes = list(extension.iter_index_records(source, IndexContext()))
    view = next(node for node in nodes if node.kind == "database_table")
    assert view.title == VIEW
    assert view.attributes["object_kind"] == "view"
    assert view.attributes["primary_key_columns"] == []

    rows = ask(extension, source, "open.rows", {})
    assert rows.evidence_kind is EvidenceKind.OBSERVED
    assert [
        (
            row[f"{VIEW}__contract_id"],
            row[f"{VIEW}__invoice_id"],
            row[f"{VIEW}__amount_cents"],
        )
        for row in rows.value["rows"]
    ] == OPEN_TENANT_A
    totals = ask(extension, source, "open.totals", {})
    assert totals.evidence_kind is EvidenceKind.CALCULATED
    assert totals.value["rows"] == [
        {f"{VIEW}__contract_id": 1, "open_cents": 220000},
        {f"{VIEW}__contract_id": 2, "open_cents": 90000},
    ]


# DuckDB files -----------------------------------------------------------------


@pytest.fixture
def duckdb_file(tmp_path: Path) -> Path:
    duckdb = pytest.importorskip("duckdb")
    path = tmp_path / "contracts.duckdb"
    connection = duckdb.connect(str(path))
    try:
        for statement in fixture_statements("duckdb"):
            connection.execute(statement)
        connection.executemany("INSERT INTO contracts VALUES (?, ?, ?, ?)", CONTRACTS)
        connection.executemany(
            "INSERT INTO invoices VALUES (?, ?, ?, ?, ?, ?, ?)", INVOICES
        )
    finally:
        connection.close()
    return path


def test_duckdb_file_meets_the_shared_contract(duckdb_file: Path) -> None:
    extension = DatabaseExtension()
    version = extension.database_source_version(duckdb_file)
    assert version.startswith("duckdb-state:")
    source = SourceRef("contracts-duckdb", "application/vnd.duckdb", version)
    extension.register_database(
        source,
        duckdb_file,
        allowed_columns=ALLOWED_COLUMNS,
        queries=query_definitions(),
    )

    assert_relational_contract(extension, source, "duckdb")
    evidence = ask(extension, source, "invoices.overdue_count", FILTERS)
    assert evidence.locator.value["database_file_state_sha256"]
    extension.close()


def test_duckdb_changes_and_mismatched_file_formats_are_rejected(
    duckdb_file: Path, tmp_path: Path
) -> None:
    duckdb = pytest.importorskip("duckdb")
    extension = DatabaseExtension()
    source = SourceRef("contracts-duckdb", "application/vnd.duckdb", "v1")
    extension.register_database(
        source,
        duckdb_file,
        allowed_columns=ALLOWED_COLUMNS,
        queries=query_definitions(),
    )
    connection = duckdb.connect(str(duckdb_file))
    try:
        connection.execute(
            "UPDATE invoices SET paid = true WHERE invoice_id = 'INV-1001'"
        )
    finally:
        connection.close()
    with pytest.raises(DatabaseSourceChangedError):
        ask(extension, source, "invoices.overdue_count", FILTERS)
    extension.close()

    sqlite_file = create_database(tmp_path / "contracts.sqlite")
    with pytest.raises(DatabaseRegistrationError, match="not a DuckDB file"):
        DatabaseExtension().register_database(
            SourceRef("as-duckdb", "application/vnd.duckdb", "v1"),
            sqlite_file,
            allowed_columns=ALLOWED_COLUMNS,
            queries=query_definitions(),
        )
    with pytest.raises(DatabaseRegistrationError):
        DatabaseExtension().register_database(
            SourceRef("as-sqlite", "application/vnd.sqlite3", "v1"),
            duckdb_file,
            allowed_columns=ALLOWED_COLUMNS,
            queries=query_definitions(),
        )


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM read_csv('{csv}')",
        "COPY contracts TO '{out}'",
        "ATTACH '{other}' AS other",
        "SET enable_external_access = true",
        "SET threads = 4",
        "INSERT INTO contracts VALUES (9, 'Injected', 'USD', 'tenant-a')",
    ],
)
def test_duckdb_sessions_cannot_reach_files_change_settings_or_write(
    duckdb_file: Path, tmp_path: Path, statement: str
) -> None:
    duckdb = pytest.importorskip("duckdb")
    csv_file = tmp_path / "rows.csv"
    csv_file.write_text("a\n1\n", encoding="utf-8")
    sql = statement.format(
        csv=csv_file.as_posix(),
        out=(tmp_path / "out.csv").as_posix(),
        other=(tmp_path / "other.duckdb").as_posix(),
    )
    connection = duckdb_backend.open_read_only(duckdb_file)
    try:
        with pytest.raises(duckdb.Error):
            connection.execute(sql).fetchall()
    finally:
        connection.close()
    assert not (tmp_path / "out.csv").exists()
    assert not (tmp_path / "other.duckdb").exists()


def duckdb_execute(path: Path, statements: Sequence[str]) -> None:
    duckdb = pytest.importorskip("duckdb")
    connection = duckdb.connect(str(path))
    try:
        for statement in statements:
            connection.execute(statement)
    finally:
        connection.close()


def register_duckdb_probe(path: Path, columns: Mapping[str, Sequence[str]]) -> int:
    """Register one keyless object and count its rows."""
    (name,) = columns
    source = SourceRef("probe-duckdb", "application/vnd.duckdb", "v1")
    extension = DatabaseExtension()
    try:
        extension.register_database(
            source,
            path,
            allowed_columns=columns,
            queries=(
                QueryDefinition(
                    "probe.count",
                    name,
                    (),
                    aggregate=AggregateRule("count", None, "probe_rows"),
                    max_rows=1,
                ),
            ),
        )
        rows = ask(extension, source, "probe.count", {}).value["rows"]
    finally:
        extension.close()
    return int(rows[0]["probe_rows"])


PROBE = {"probe": ("contract_id", "amount_cents", "tenant_id")}


def test_duckdb_views_meet_the_view_contract(duckdb_file: Path) -> None:
    extension = DatabaseExtension()
    source = SourceRef("open-duckdb", "application/vnd.duckdb", "v1")
    extension.register_database(
        source, duckdb_file, allowed_columns=VIEW_COLUMNS, queries=view_queries()
    )

    assert_view_contract(extension, source)
    extension.close()


@pytest.mark.parametrize(
    ("statements", "count"),
    [
        (
            [
                "CREATE VIEW probe AS "
                "SELECT contract_id, amount_cents, tenant_id FROM open_invoices"
            ],
            5,
        ),
        (
            [
                "CREATE VIEW probe AS WITH o AS (SELECT * FROM invoices WHERE NOT paid) "
                "SELECT o.contract_id, o.amount_cents, upper(c.tenant_id) AS tenant_id "
                "FROM o JOIN contracts AS c ON c.contract_id = o.contract_id"
            ],
            5,
        ),
        (
            [
                "CREATE VIEW probe AS SELECT contract_id, "
                "row_number() OVER (ORDER BY invoice_id) AS amount_cents, tenant_id "
                "FROM invoices UNION ALL SELECT * FROM (VALUES (9, 0, 'x')) "
                "AS v(contract_id, amount_cents, tenant_id)"
            ],
            7,
        ),
    ],
)
def test_duckdb_views_over_main_tables_and_consistent_functions_are_accepted(
    duckdb_file: Path, statements: list[str], count: int
) -> None:
    duckdb_execute(duckdb_file, statements)

    assert register_duckdb_probe(duckdb_file, PROBE) == count


@pytest.mark.parametrize(
    ("statements", "reason"),
    [
        (
            [
                "CREATE VIEW probe AS SELECT contract_id, amount_cents, "
                "current_setting('threads')::VARCHAR AS tenant_id FROM invoices"
            ],
            "'current_setting'",
        ),
        (
            [
                "CREATE VIEW probe AS SELECT contract_id, amount_cents, "
                "CAST(now() AS VARCHAR) AS tenant_id FROM invoices"
            ],
            "'now'",
        ),
        (
            [
                "CREATE VIEW probe AS SELECT contract_id, amount_cents, "
                "CAST(current_localtimestamp() AS VARCHAR) AS tenant_id FROM invoices"
            ],
            "'current_localtimestamp'",
        ),
        (
            [
                "CREATE VIEW probe AS SELECT 1 AS contract_id, 2 AS amount_cents, "
                "value AS tenant_id FROM duckdb_settings()"
            ],
            "table function",
        ),
        (
            [
                "CREATE VIEW probe AS SELECT range AS contract_id, "
                "range AS amount_cents, 'x' AS tenant_id FROM range(3)"
            ],
            "table function",
        ),
        (
            [
                "CREATE VIEW probe AS SELECT 1 AS contract_id, 2 AS amount_cents, "
                "sql AS tenant_id FROM sqlite_master"
            ],
            "outside the file's main schema",
        ),
        (
            [
                "CREATE SCHEMA other",
                "CREATE TABLE other.t AS "
                "SELECT contract_id, amount_cents, tenant_id FROM invoices",
                "CREATE VIEW probe AS SELECT * FROM other.t",
            ],
            "outside the file's main schema",
        ),
        (
            [
                "CREATE MACRO leak() AS current_setting('threads')",
                "CREATE VIEW probe AS SELECT contract_id, amount_cents, "
                "leak()::VARCHAR AS tenant_id FROM invoices",
            ],
            "'leak'",
        ),
        (
            [
                "CREATE TABLE gen (contract_id INTEGER, amount_cents BIGINT, "
                "tenant_id VARCHAR GENERATED ALWAYS AS "
                "(current_setting('threads')::VARCHAR))",
                "INSERT INTO gen (contract_id, amount_cents) VALUES (1, 2)",
                "CREATE VIEW probe AS SELECT * FROM gen",
            ],
            "generated column gen.tenant_id",
        ),
        (
            [
                "CREATE VIEW probe AS SELECT contract_id, amount_cents, tenant_id "
                "FROM invoices USING SAMPLE 50%",
            ],
            "samples rows",
        ),
    ],
)
def test_duckdb_views_that_reach_settings_files_or_other_schemas_are_refused(
    duckdb_file: Path, statements: list[str], reason: str
) -> None:
    duckdb_execute(duckdb_file, statements)

    with pytest.raises(DatabaseRegistrationError, match=reason):
        register_duckdb_probe(duckdb_file, PROBE)


def test_duckdb_generated_columns_are_checked_only_when_allowlisted(
    duckdb_file: Path,
) -> None:
    duckdb_execute(
        duckdb_file,
        [
            "CREATE TABLE gen (gen_id INTEGER PRIMARY KEY, amount_cents BIGINT, "
            "doubled BIGINT GENERATED ALWAYS AS (amount_cents * 2), "
            "threads VARCHAR GENERATED ALWAYS AS (current_setting('threads')::VARCHAR))",
            "INSERT INTO gen (gen_id, amount_cents) VALUES (1, 2)",
        ],
    )

    assert register_duckdb_probe(duckdb_file, {"gen": ("gen_id", "doubled")}) == 1
    with pytest.raises(DatabaseRegistrationError, match="generated column gen.threads"):
        register_duckdb_probe(duckdb_file, {"gen": ("gen_id", "threads")})


# SQL servers through SQLAlchemy -------------------------------------------------


@pytest.fixture
def sqlalchemy_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Serve SQLite through the SQLAlchemy server backend, as a real server would be."""
    pytest.importorskip("sqlalchemy")
    monkeypatch.setitem(
        connections_module.ENGINES,
        TEST_ENGINE,
        EngineSpec(
            name=TEST_ENGINE,
            label="SQLite test server",
            family="sqlalchemy",
            drivers={"sqlite+pysqlite": ("sqlalchemy", "sqlite3")},
            aliases={},
            extra=TEST_ENGINE,
            query_parameters=frozenset(),
            tier="integration",
            password=False,
            schema=False,
            host_required=False,
            database_required=True,
        ),
    )
    monkeypatch.setattr(connections_module, "_FILE_SCHEMES", {"duckdb": "DuckDB"})
    monkeypatch.setattr(
        plugin_module,
        "SUPPORTED_SOURCE_TYPES",
        plugin_module.SUPPORTED_SOURCE_TYPES | {TEST_SOURCE_TYPE},
    )
    monkeypatch.chdir(tmp_path)
    return create_database(tmp_path / "server.db")


def alter(path: Path, statement: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(statement)
        connection.commit()
    finally:
        connection.close()


def test_sqlalchemy_server_meets_the_shared_contract(sqlalchemy_server: Path) -> None:
    extension = DatabaseExtension()
    config = connection_config(TEST_CONNECTION)
    state = extension.connection_source_version(config)
    assert state.startswith(f"{TEST_ENGINE}-schema:")
    source = SourceRef("db-server", TEST_SOURCE_TYPE, state)

    assert extension.register_connection_config(source, config, expected_state=state) == (
        state
    )
    assert_relational_contract(extension, source, TEST_ENGINE)
    root = next(extension.iter_index_records(source, IndexContext()))
    connection_id = parse_connection(TEST_CONNECTION).connection_id
    assert root.attributes["connection_id"] == connection_id
    assert root.attributes["support_tier"] == "integration"
    evidence = ask(extension, source, "invoices.overdue_count", FILTERS)
    assert evidence.locator.value["connection_id"] == connection_id
    assert "database_file_state_sha256" not in evidence.locator.value
    extension.close()


def test_server_schema_changes_are_detected_before_indexing_and_registration(
    sqlalchemy_server: Path,
) -> None:
    extension = DatabaseExtension()
    config = connection_config(TEST_CONNECTION)
    state = extension.connection_source_version(config)
    source = SourceRef("db-server", TEST_SOURCE_TYPE, state)
    extension.register_connection_config(source, config)

    alter(sqlalchemy_server, "ALTER TABLE invoices ADD COLUMN note TEXT")

    with pytest.raises(DatabaseSchemaChangedError):
        list(extension.iter_index_records(source, IndexContext()))
    changed = extension.connection_source_version(config)
    assert changed != state
    resubmitted = SourceRef("db-server", TEST_SOURCE_TYPE, changed)
    with pytest.raises(DatabaseSourceChangedError, match="submit it again"):
        DatabaseExtension().register_connection_config(
            resubmitted, config, expected_state=state
        )
    assert (
        DatabaseExtension().register_connection_config(
            resubmitted, config, expected_state=changed
        )
        == changed
    )
    extension.close()


def test_server_views_answer_rows_ordered_by_every_selected_column(
    sqlalchemy_server: Path,
) -> None:
    alter(sqlalchemy_server, view_statement(TEST_ENGINE))
    partial = QueryDefinition(
        "open.rows",
        VIEW,
        (ColumnRef(VIEW, "contract_id"), ColumnRef(VIEW, "invoice_id")),
        order_by=(ColumnRef(VIEW, "contract_id"),),
    )
    source = SourceRef("db-view", TEST_SOURCE_TYPE, "v1")
    extension = DatabaseExtension()

    with pytest.raises(InvalidDatabaseConfigurationError, match="every selected column"):
        extension.register_connection(
            source, TEST_CONNECTION, allowed_columns=VIEW_COLUMNS, queries=(partial,)
        )
    extension.register_connection(
        source, TEST_CONNECTION, allowed_columns=VIEW_COLUMNS, queries=view_queries()
    )

    assert_view_contract(extension, source)
    extension.close()


def test_server_connection_errors_never_reveal_the_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sa = pytest.importorskip("sqlalchemy")
    password = "p@ss:w/rd"
    monkeypatch.setattr(connections_module, "_importable", lambda module: True)
    monkeypatch.setenv("DATAINDEX_PG_PW", password)
    seen: list[tuple[object, ...]] = []

    def refuse(url: Any, **options: Any) -> None:
        seen.append((url.password, options["poolclass"], options["hide_parameters"]))
        raise RuntimeError(
            "FATAL: password authentication failed for "
            f"{url.render_as_string(hide_password=False)} ({url.password})"
        )

    monkeypatch.setattr(sa, "create_engine", refuse)
    extension = DatabaseExtension()
    with pytest.raises(DatabaseConnectionError) as caught:
        extension.register_connection(
            SourceRef("db-pg", "application/vnd.dataindex.db.postgresql", "v1"),
            {
                "url": "postgresql://reader@db.example.com/sales",
                "password_env": "DATAINDEX_PG_PW",
            },
            allowed_columns=ALLOWED_COLUMNS,
            queries=query_definitions(),
        )

    message = str(caught.value)
    assert message.startswith(
        "could not connect to postgresql+psycopg://reader@db.example.com/sales: "
        "RuntimeError: FATAL"
    )
    assert "ss:w" not in message
    assert "%40ss" not in message
    assert seen == [(password, sa.pool.NullPool, True)]


@pytest.mark.parametrize(
    ("module", "url", "db_schema", "limit_keyword"),
    [
        ("sqlalchemy", "postgresql://", "finance", "LIMIT"),
        ("sqlalchemy", "mysql://", "finance", "LIMIT"),
        ("sqlalchemy", "mssql://", "finance", "TOP"),
        ("sqlalchemy", "oracle://", "finance", "FETCH FIRST"),
        ("snowflake.sqlalchemy", "snowflake://", "PUBLIC", "LIMIT"),
        ("sqlalchemy_bigquery", "bigquery://", None, "LIMIT"),
        ("clickhouse_connect", "clickhousedb://", None, "LIMIT"),
    ],
)
def test_approved_queries_compile_with_bound_values_for_each_dialect(
    module: str, url: str, db_schema: str | None, limit_keyword: str
) -> None:
    sa = pytest.importorskip("sqlalchemy")
    pytest.importorskip(module)
    dialect = sa.engine.make_url(url).get_dialect()()
    types = {
        ("invoices", "due_date"): sa.Date(),
        ("invoices", "paid"): sa.Boolean(),
        ("invoices", "amount_cents"): sa.BigInteger(),
        ("invoices", "tenant_id"): sa.String(64),
        ("contracts", "tenant_id"): sa.String(64),
    }
    query = query_definitions()[1]

    statement = build_statement(
        sa, query, bind_filters(query, FILTERS, 25), 25, types, db_schema
    )
    compiled = statement.compile(dialect=dialect)
    sql = str(compiled)

    assert limit_keyword in sql.upper()
    assert "JOIN" in sql.upper()
    assert "GROUP BY" in sql.upper()
    assert "tenant-a" not in sql
    assert "2026" not in sql
    assert date(2026, 2, 28) in compiled.params.values()
    assert "tenant-a" in compiled.params.values()
    if db_schema is not None:
        assert db_schema in sql
    with pytest.raises(InvalidDatabaseToolRequestError, match="column type"):
        build_statement(
            sa,
            query,
            bind_filters(query, {**FILTERS, "due_before": "end of month"}, 25),
            25,
            types,
            db_schema,
        )


class _FakeSession:
    """Records the statements a read-only guard issues."""

    def __init__(self, state: object, version: tuple[int, ...] = ()) -> None:
        self.state = state
        self.statements: list[str] = []
        self.raw = SimpleNamespace(rollback=lambda: None, timeout=0, call_timeout=0)
        self.connection = SimpleNamespace(dbapi_connection=self.raw)
        self.dialect = SimpleNamespace(is_mariadb=False, server_version_info=version)

    def exec_driver_sql(self, statement: str) -> Any:
        self.statements.append(statement)
        return SimpleNamespace(scalar=lambda: self.state)

    def rollback(self) -> None:
        self.statements.append("ROLLBACK")


@pytest.mark.parametrize(
    ("engine", "state", "version", "fragments", "raw_settings"),
    [
        (
            "postgresql",
            "on",
            (),
            ("SET TRANSACTION READ ONLY", "'statement_timeout', '1500'"),
            {},
        ),
        (
            "mysql",
            1,
            (),
            ("SET SESSION TRANSACTION READ ONLY", "max_execution_time = 1500",
             "@@session.transaction_read_only"),
            {},
        ),
        (
            "mariadb",
            1,
            (10, 11, 6),
            ("SET SESSION TRANSACTION READ ONLY", "max_statement_time = 2",
             "@@session.tx_read_only"),
            {},
        ),
        (
            "mariadb",
            1,
            (11, 4, 2),
            ("max_statement_time = 2", "@@session.transaction_read_only"),
            {},
        ),
        ("mssql", None, (), ("SET LOCK_TIMEOUT 1500",), {"timeout": 2}),
        ("oracle", None, (), ("SET TRANSACTION READ ONLY",), {"call_timeout": 1500}),
        ("snowflake", None, (), ("STATEMENT_TIMEOUT_IN_SECONDS = 2",), {}),
    ],
)
def test_sessions_get_read_only_and_timeout_guards_per_engine(
    engine: str,
    state: object,
    version: tuple[int, ...],
    fragments: tuple[str, ...],
    raw_settings: dict[str, int],
) -> None:
    session = _FakeSession(state, version)

    apply_read_only_guards(engine, session, 1.5)

    issued = "\n".join(session.statements)
    for fragment in fragments:
        assert fragment in issued
    for name, value in raw_settings.items():
        assert getattr(session.raw, name) == value


@pytest.mark.parametrize(("engine", "state"), [("postgresql", "off"), ("mysql", 0)])
def test_sessions_that_do_not_confirm_read_only_mode_are_refused(
    engine: str, state: object
) -> None:
    with pytest.raises(DatabaseConnectionError, match="did not confirm"):
        apply_read_only_guards(engine, _FakeSession(state), 1.5)


# MongoDB --------------------------------------------------------------------------


def mongo_invoices() -> list[dict[str, object]]:
    return [
        {
            "_id": invoice_id,
            "contract_id": contract_id,
            "amount_cents": amount,
            "due_date": due.isoformat(),
            "paid": paid,
            "tenant_id": tenant,
            "reference": reference,
            "customer": {"country": COUNTRIES[invoice_id]},
        }
        for invoice_id, contract_id, amount, due, paid, tenant, reference in INVOICES
    ]


def mongo_queries() -> tuple[QueryDefinition, ...]:
    table = "invoices"

    def field(name: str) -> ColumnRef:
        return ColumnRef(table, name)

    filters = (
        FilterRule("due_before", field("due_date"), "lte"),
        FilterRule("is_paid", field("paid")),
    )
    tenant = (FixedFilterRule("tenant", field("tenant_id"), "eq", "tenant-a"),)
    return (
        QueryDefinition(
            "invoices.overdue",
            table,
            (field("_id"), field("contract_id"), field("amount_cents"), field("due_date")),
            filters=filters,
            source_filters=tenant,
            order_by=(field("due_date"), field("_id")),
            max_rows=25,
            unit="USD cents",
        ),
        QueryDefinition(
            "invoices.overdue_total_by_contract",
            table,
            (field("contract_id"),),
            filters=filters,
            source_filters=tenant,
            aggregate=AggregateRule("sum", field("amount_cents"), "overdue_total_cents"),
            group_by=(field("contract_id"),),
            max_rows=25,
            unit="USD cents",
        ),
        QueryDefinition(
            "invoices.overdue_count",
            table,
            (),
            filters=filters,
            source_filters=tenant,
            aggregate=AggregateRule("count", None, "invoice_count"),
            max_rows=1,
        ),
        QueryDefinition(
            "invoices.by_reference",
            table,
            (field("_id"), field("reference")),
            filters=(FilterRule("reference", field("reference")),),
            source_filters=tenant,
            order_by=(field("_id"),),
            max_rows=10,
        ),
        QueryDefinition(
            "invoices.by_country",
            table,
            (field("_id"), field("customer.country")),
            filters=(FilterRule("country", field("customer.country")),),
            source_filters=tenant,
            order_by=(field("_id"),),
            max_rows=10,
        ),
    )


def assert_mongo_contract(extension: DatabaseExtension, source: SourceRef) -> None:
    nodes = list(extension.iter_index_records(source, IndexContext()))
    assert nodes[0].locator.scheme == "mongodb-schema"
    collection = next(node for node in nodes if node.kind == "database_table")
    assert collection.attributes["object_kind"] == "collection"
    assert collection.attributes["primary_key_columns"] == ["_id"]
    fields = {node.title: node.attributes for node in nodes if node.kind == "database_column"}
    assert fields["invoices.amount_cents"]["declared_type"] == "int|long"
    assert fields["invoices.amount_cents"]["nullable"] is False
    assert fields["invoices.customer.country"]["nullable"] is True
    assert not [node for node in nodes if node.kind == "database_foreign_key"]
    assert "INV-1001" not in json.dumps([node.attributes for node in nodes])

    overdue = ask(extension, source, "invoices.overdue", FILTERS)
    assert overdue.locator.scheme == "mongodb-query"
    assert overdue.value["rows"] == [
        {
            "invoices___id": "INV-1001",
            "invoices__contract_id": 1,
            "invoices__amount_cents": 125000,
            "invoices__due_date": "2026-01-15",
        },
        {
            "invoices___id": "INV-1002",
            "invoices__contract_id": 1,
            "invoices__amount_cents": 25000,
            "invoices__due_date": "2026-02-01",
        },
        {
            "invoices___id": "INV-2001",
            "invoices__contract_id": 2,
            "invoices__amount_cents": 90000,
            "invoices__due_date": "2026-02-10",
        },
    ]
    totals = ask(extension, source, "invoices.overdue_total_by_contract", FILTERS)
    assert totals.evidence_kind is EvidenceKind.CALCULATED
    assert totals.value["rows"] == [
        {"invoices__contract_id": 1, "overdue_total_cents": 150000},
        {"invoices__contract_id": 2, "overdue_total_cents": 90000},
    ]
    count = ask(extension, source, "invoices.overdue_count", FILTERS)
    assert count.value["rows"] == [{"invoice_count": 3}]
    german = ask(extension, source, "invoices.by_country", {"country": "DE"})
    assert german.value["rows"] == [
        {"invoices___id": "INV-2001", "invoices__customer.country": "DE"},
        {"invoices___id": "INV-2002", "invoices__customer.country": "DE"},
    ]
    for reference in ("' OR 1=1 --", "CT-2026-01"):
        found = ask(extension, source, "invoices.by_reference", {"reference": reference})
        assert found.value["rows"] == []
    with pytest.raises(InvalidDatabaseToolRequestError, match="scalar"):
        ask(extension, source, "invoices.by_reference", {"reference": {"$ne": ""}})


@pytest.fixture
def mongo_server(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Serve ``mongomock`` through the MongoDB backend in place of a server.

    ``mongomock`` has no views, so ``open_invoices`` is stored as the rows the
    server would compute from its pipeline and reported as a view.
    """
    mongomock = pytest.importorskip("mongomock")
    pytest.importorskip("pymongo")
    client = mongomock.MongoClient()
    client["app"]["invoices"].insert_many(mongo_invoices())
    client["app"]["audit"].insert_one({"_id": 1, "contract_id": 1})
    client["app"][VIEW].insert_many(
        [
            {
                "invoice_id": document["_id"],
                **{key: document[key] for key in VIEW_COLUMNS[VIEW][1:]},
            }
            for document in mongo_invoices()
            if not document["paid"]
        ]
    )
    validators = {"invoices": MONGO_VALIDATOR}
    views: dict[str, dict[str, object]] = {
        VIEW: {"viewOn": "invoices", "pipeline": OPEN_PIPELINE}
    }
    passwords: list[str | None] = []

    class Client:
        def __getitem__(self, name: str) -> Any:
            return client[name]

        def close(self) -> None:
            pass

    def open_client(
        settings: ConnectionSettings, password: str | None, timeout: float
    ) -> Client:
        passwords.append(password)
        return Client()

    def collection_infos(database: Any, names: Sequence[str]) -> dict[str, Any]:
        present = set(database.list_collection_names())
        return {
            name: (
                {"type": "view", "options": dict(views[name])}
                if name in views
                else {
                    "type": "collection",
                    "options": (
                        {"validator": validators[name]} if name in validators else {}
                    ),
                }
            )
            for name in names
            if name in present
        }

    monkeypatch.setattr(mongodb_backend, "open_client", open_client)
    monkeypatch.setattr(mongodb_backend, "_collection_infos", collection_infos)
    monkeypatch.setenv(MONGO_PASSWORD_ENV, MONGO_SECRET)
    return SimpleNamespace(
        connection={
            "url": "mongodb://reader@mongo.example.com:27017/app?authSource=admin",
            "password_env": MONGO_PASSWORD_ENV,
        },
        passwords=passwords,
        views=views,
    )


def test_mongodb_collections_meet_the_contract_without_leaking_the_password(
    mongo_server: SimpleNamespace,
) -> None:
    extension = DatabaseExtension()
    config = connection_config(mongo_server.connection, MONGO_ALLOWED, mongo_queries())
    state = extension.connection_source_version(config)
    assert state.startswith("mongodb-schema:")
    source = SourceRef("mongo-app", "application/vnd.dataindex.db.mongodb", state)
    extension.register_connection_config(source, config, expected_state=state)

    assert_mongo_contract(extension, source)
    assert set(mongo_server.passwords) == {MONGO_SECRET}
    published = json.dumps(
        [
            config,
            extension.describe_connection(mongo_server.connection),
            [node.attributes for node in extension.iter_index_records(source, IndexContext())],
            ask(extension, source, "invoices.overdue", FILTERS).value,
        ]
    )
    assert MONGO_SECRET not in published
    extension.close()


def test_mongodb_views_meet_the_view_contract_and_pipeline_changes_are_detected(
    mongo_server: SimpleNamespace,
) -> None:
    extension = DatabaseExtension()
    config = connection_config(mongo_server.connection, VIEW_COLUMNS, view_queries())
    state = extension.connection_source_version(config)
    source = SourceRef("mongo-open", "application/vnd.dataindex.db.mongodb", state)
    extension.register_connection_config(source, config, expected_state=state)

    assert_view_contract(extension, source)
    mongo_server.views[VIEW] = {
        "viewOn": "invoices",
        "pipeline": [{"$match": {}}, *OPEN_PIPELINE[1:]],
    }
    assert extension.connection_source_version(config) != state
    with pytest.raises(DatabaseSchemaChangedError):
        list(extension.iter_index_records(source, IndexContext()))
    extension.close()


def test_mongodb_rejects_joins_system_collections_and_unsafe_field_paths(
    mongo_server: SimpleNamespace,
) -> None:
    connection = mongo_server.connection
    source = SourceRef("mongo-app", "application/vnd.dataindex.db.mongodb", "v1")
    count = mongo_queries()[2]
    joined = QueryDefinition(
        "invoices.audited",
        "invoices",
        (),
        joins=(JoinRule(ColumnRef("invoices", "contract_id"), ColumnRef("audit", "_id")),),
        aggregate=AggregateRule("count", None, "audited"),
        max_rows=1,
    )
    cases: list[tuple[Mapping[str, Sequence[str]], QueryDefinition, type[Exception], str]] = [
        (
            {**MONGO_ALLOWED, "audit": ("_id",)},
            joined,
            InvalidDatabaseConfigurationError,
            "do not support",
        ),
        ({"system.users": ("_id",)}, count, InvalidDatabaseConfigurationError, "internal"),
        (
            {"invoices": ("_id", "$where", "paid", "due_date", "tenant_id")},
            count,
            InvalidDatabaseConfigurationError,
            "dotted names",
        ),
        (
            {"invoices": ("_id", "customer", "customer.country")},
            count,
            InvalidDatabaseConfigurationError,
            "overlaps",
        ),
        ({"missing": ("_id",)}, count, DatabaseRegistrationError, "absent"),
    ]
    for allowed, query, error, message in cases:
        with pytest.raises(error, match=message):
            DatabaseExtension().register_connection(
                source, connection, allowed_columns=allowed, queries=(query,)
            )


def test_mongodb_filter_values_follow_declared_types_and_sql_null_semantics() -> None:
    bson = pytest.importorskip("bson")
    object_id = "65a1b2c3d4e5f60718293a4b"

    assert coerce_value("2026-02-28", ("date",)) == datetime(
        2026, 2, 28, tzinfo=UTC
    )
    assert coerce_value(object_id, ("objectId",)) == bson.ObjectId(object_id)
    assert coerce_value("12.50", ("decimal",)) == bson.Decimal128("12.50")
    assert coerce_value("12.50", ("string",)) == "12.50"
    assert coerce_value("12.50", ("decimal", "null")) == "12.50"
    for value, kind in (("not-an-id", "objectId"), ("soon", "date"), ("NaN", "decimal")):
        with pytest.raises(InvalidDatabaseToolRequestError):
            coerce_value(value, (kind,))
    # SQL "<>" never matches NULL, so "ne" must not match missing fields either.
    reference = ColumnRef("invoices", "reference")
    assert build_match((BoundFilter(reference, "ne", ("A",)),), {}, "invoices") == {
        "$and": [{"reference": {"$nin": ["A", None]}}]
    }


# Live servers ---------------------------------------------------------------------


def live_connection(engine: str) -> dict[str, str]:
    prefix = f"DATAINDEX_TEST_{engine.upper()}"
    url = os.environ.get(f"{prefix}_URL")
    if not url:
        pytest.skip(f"set {prefix}_URL to run the live {engine} test")
    connection = {"url": url}
    if os.environ.get(f"{prefix}_PASSWORD"):
        connection["password_env"] = f"{prefix}_PASSWORD"
    return connection


def load_live_sql_fixture(settings: ConnectionSettings) -> None:
    sa = pytest.importorskip("sqlalchemy")
    url = sa.engine.make_url(settings.url)
    password = settings.password()
    if password is not None:
        url = url.set(password=password)
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    try:
        with engine.begin() as connection:
            for statement in fixture_statements(settings.engine.name):
                connection.exec_driver_sql(statement)
            connection.execute(
                sa.text("INSERT INTO contracts VALUES (:k, :n, :c, :t)"),
                [dict(zip(("k", "n", "c", "t"), row, strict=True)) for row in CONTRACTS],
            )
            connection.execute(
                sa.text("INSERT INTO invoices VALUES (:i, :k, :a, :d, :p, :t, :r)"),
                [
                    dict(zip(("i", "k", "a", "d", "p", "t", "r"), row, strict=True))
                    for row in INVOICES
                ],
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("engine", LIVE_SQL_ENGINES)
def test_live_sql_server_meets_the_shared_contract(engine: str) -> None:
    connection = live_connection(engine)
    settings = parse_connection(connection)
    assert settings.engine.name == engine
    load_live_sql_fixture(settings)
    extension = DatabaseExtension()
    config = connection_config(connection)
    state = extension.connection_source_version(config)
    source = SourceRef(f"live-{engine}", settings.engine.source_type, state)
    extension.register_connection_config(source, config, expected_state=state)

    assert_relational_contract(extension, source, engine)
    if engine in READ_ONLY_TRANSACTION_ENGINES:
        with (
            pytest.raises(DatabaseConnectionError, match="could not write to"),
            SQLAlchemyServer(settings)._session("write to", None) as (session, _),
        ):
            session.exec_driver_sql("DELETE FROM invoices")
        count = ask(extension, source, "invoices.overdue_count", FILTERS)
        assert count.value["rows"] == [{"invoice_count": 3}]
    extension.close()

    view_config = connection_config(connection, VIEW_COLUMNS, view_queries())
    views = DatabaseExtension()
    view_state = views.connection_source_version(view_config)
    view_source = SourceRef(f"live-{engine}-view", settings.engine.source_type, view_state)
    views.register_connection_config(view_source, view_config, expected_state=view_state)
    assert_view_contract(views, view_source)
    views.close()


def test_live_mongodb_meets_the_contract() -> None:
    connection = live_connection("mongodb")
    pytest.importorskip("pymongo")
    settings = parse_connection(connection)
    client = mongodb_backend.open_client(settings, settings.password(), 10.0)
    try:
        database = client[settings.database]
        database.drop_collection(VIEW)
        database.drop_collection("invoices")
        database.create_collection("invoices", validator=MONGO_VALIDATOR)
        database["invoices"].insert_many(mongo_invoices())
        database.create_collection(VIEW, viewOn="invoices", pipeline=OPEN_PIPELINE)
    finally:
        client.close()
    extension = DatabaseExtension()
    config = connection_config(connection, MONGO_ALLOWED, mongo_queries())
    state = extension.connection_source_version(config)
    source = SourceRef("live-mongodb", settings.engine.source_type, state)
    extension.register_connection_config(source, config, expected_state=state)

    assert_mongo_contract(extension, source)
    extension.close()

    view_config = connection_config(connection, VIEW_COLUMNS, view_queries())
    views = DatabaseExtension()
    view_state = views.connection_source_version(view_config)
    view_source = SourceRef("live-mongodb-view", settings.engine.source_type, view_state)
    views.register_connection_config(view_source, view_config, expected_state=view_state)
    assert_view_contract(views, view_source)
    views.close()
