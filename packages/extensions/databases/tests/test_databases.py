from __future__ import annotations

import json
import os
import sqlite3
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from dataindex.models import (
    EvidenceKind,
    IndexContext,
    RetrievalContext,
    SourceRef,
    ToolRequest,
)
from dataindex.registry import ENTRY_POINT_GROUP, ExtensionRegistry
from dataindex_ext_databases import (
    AggregateRule,
    ColumnRef,
    DatabaseDeadlineExceededError,
    DatabaseExecutionBudgetExceededError,
    DatabaseRegistrationError,
    DatabaseResultTooLargeError,
    DatabaseSchemaChangedError,
    DatabaseSourceChangedError,
    FixedFilterRule,
    FilterRule,
    InvalidDatabaseConfigurationError,
    InvalidDatabaseToolRequestError,
    JoinRule,
    QueryDefinition,
    SQLiteDatabaseExtension,
    UnauthorizedDatabaseAccessError,
)
from dataindex_ext_databases.plugin import QUERY_TOOL_ID
from dataindex_ext_databases.queries import build_query
from dataindex_ext_databases.sqlite import open_read_only

FIXTURE = Path(__file__).parents[1] / "examples" / "fixtures" / "contracts_invoices.sql"
ALLOWED_COLUMNS = {
    "contracts": ("contract_id", "contract_name", "currency", "tenant_id"),
    "invoices": (
        "invoice_id",
        "contract_id",
        "amount_cents",
        "due_date",
        "paid",
        "tenant_id",
        "reference",
    ),
}


def source(
    source_id: str = "demo-contracts",
    version: str = "sqlite-live:fixture-v1",
) -> SourceRef:
    return SourceRef(source_id, "application/vnd.sqlite3", version)


def query_definitions() -> tuple[QueryDefinition, ...]:
    invoices = "invoices"
    contracts = "contracts"
    due_date = ColumnRef(invoices, "due_date")
    paid = ColumnRef(invoices, "paid")
    invoice_tenant = ColumnRef(invoices, "tenant_id")
    contract_tenant = ColumnRef(contracts, "tenant_id")
    invoice_id = ColumnRef(invoices, "invoice_id")
    contract_id = ColumnRef(contracts, "contract_id")
    contract_name = ColumnRef(contracts, "contract_name")
    return (
        QueryDefinition(
            query_id="invoices.overdue",
            table=invoices,
            columns=(
                invoice_id,
                ColumnRef(invoices, "contract_id"),
                ColumnRef(invoices, "amount_cents"),
                due_date,
            ),
            filters=(
                FilterRule("due_before", due_date, "lte"),
                FilterRule("is_paid", paid),
            ),
            source_filters=(
                FixedFilterRule("invoice_tenant", invoice_tenant, "eq", "tenant-a"),
            ),
            order_by=(due_date, invoice_id),
            max_rows=25,
            unit="USD cents",
        ),
        QueryDefinition(
            query_id="invoices.overdue_total_by_contract",
            table=invoices,
            columns=(contract_id, contract_name),
            filters=(
                FilterRule("due_before", due_date, "lte"),
                FilterRule("is_paid", paid),
            ),
            source_filters=(
                FixedFilterRule("invoice_tenant", invoice_tenant, "eq", "tenant-a"),
                FixedFilterRule("contract_tenant", contract_tenant, "eq", "tenant-a"),
            ),
            joins=(
                JoinRule(
                    ColumnRef(invoices, "contract_id"),
                    contract_id,
                ),
            ),
            aggregate=AggregateRule(
                "sum",
                ColumnRef(invoices, "amount_cents"),
                "overdue_total_cents",
            ),
            group_by=(contract_id, contract_name),
            max_rows=25,
            unit="USD cents",
        ),
        QueryDefinition(
            query_id="invoices.overdue_count",
            table=invoices,
            columns=(),
            filters=(
                FilterRule("due_before", due_date, "lte"),
                FilterRule("is_paid", paid),
            ),
            source_filters=(
                FixedFilterRule("invoice_tenant", invoice_tenant, "eq", "tenant-a"),
            ),
            aggregate=AggregateRule("count", None, "invoice_count"),
            max_rows=1,
        ),
        QueryDefinition(
            query_id="invoices.by_reference",
            table=invoices,
            columns=(invoice_id, ColumnRef(invoices, "reference")),
            filters=(
                FilterRule(
                    "reference",
                    ColumnRef(invoices, "reference"),
                ),
            ),
            source_filters=(
                FixedFilterRule("invoice_tenant", invoice_tenant, "eq", "tenant-a"),
            ),
            order_by=(invoice_id,),
            max_rows=10,
        ),
    )


def create_database(path: Path) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(FIXTURE.read_text(encoding="utf-8"))
    finally:
        connection.close()
    return path


def registered_extension(path: Path) -> SQLiteDatabaseExtension:
    extension = SQLiteDatabaseExtension()
    extension.register_database(
        source(),
        path,
        allowed_columns=ALLOWED_COLUMNS,
        queries=query_definitions(),
    )
    return extension


def test_source_tool_schema_exposes_only_approved_query_ids(tmp_path: Path) -> None:
    extension = registered_extension(create_database(tmp_path / "contracts.sqlite"))

    assert extension.tools_for_source(source("not-registered")) == ()
    tools = extension.tools_for_source(source())

    assert len(tools) == 1
    properties = tools[0].input_schema["properties"]
    assert isinstance(properties, dict)
    query_id_schema = properties["query_id"]
    assert isinstance(query_id_schema, dict)
    assert query_id_schema["enum"] == [
        "invoices.by_reference",
        "invoices.overdue",
        "invoices.overdue_count",
        "invoices.overdue_total_by_contract",
    ]
    extension.close()


def test_source_tool_schema_describes_approved_queries_without_scope_values(
    tmp_path: Path,
) -> None:
    extension = registered_extension(create_database(tmp_path / "contracts.sqlite"))

    description = extension.tools_for_source(source())[0].input_schema["description"]

    assert isinstance(description, str)
    assert "- invoices.overdue_total_by_contract:" in description
    assert "SUM(invoices.amount_cents) AS overdue_total_cents" in description
    assert "due_before (lte invoices.due_date)" in description
    assert "unit: USD cents" in description
    assert "tenant-a" not in description
    extension.close()


def test_query_descriptions_stay_within_the_tool_schema_size_limit(
    tmp_path: Path,
) -> None:
    invoice_id = ColumnRef("invoices", "invoice_id")
    queries = tuple(
        QueryDefinition(
            query_id=f"invoices.by_reference_variant_{index:03d}",
            table="invoices",
            columns=tuple(ColumnRef("invoices", name) for name in ALLOWED_COLUMNS["invoices"]),
            filters=(FilterRule("reference", ColumnRef("invoices", "reference")),),
            order_by=(invoice_id,),
            max_rows=25,
        )
        for index in range(100)
    )
    extension = SQLiteDatabaseExtension()
    extension.register_database(
        source(),
        create_database(tmp_path / "contracts.sqlite"),
        allowed_columns=ALLOWED_COLUMNS,
        queries=queries,
    )

    schema = extension.tools_for_source(source())[0].input_schema

    encoded = json.dumps(schema, ensure_ascii=True, separators=(",", ":"))
    assert len(encoded.encode("utf-8")) < 32 * 1024
    assert "more approved queries are not listed" in str(schema["description"])
    extension.close()


def test_query_definitions_round_trip_through_json_mappings() -> None:
    scoped = QueryDefinition(
        "invoices.scoped",
        "invoices",
        (ColumnRef("invoices", "invoice_id"),),
        source_filters=(
            FixedFilterRule(
                "tenants",
                ColumnRef("invoices", "tenant_id"),
                "in",
                ("tenant-a", "tenant-b"),
            ),
        ),
        order_by=(ColumnRef("invoices", "invoice_id"),),
    )

    for query in (*query_definitions(), scoped):
        restored = QueryDefinition.from_mapping(
            json.loads(json.dumps(query.to_mapping()))
        )
        assert restored == query


def test_query_mappings_reject_unknown_fields_and_wrong_types() -> None:
    good = query_definitions()[0].to_mapping()

    with pytest.raises(ValueError, match="unsupported fields"):
        QueryDefinition.from_mapping({**good, "sql": "SELECT 1"})
    with pytest.raises(ValueError, match="query_id"):
        QueryDefinition.from_mapping({**good, "query_id": 7})
    with pytest.raises(ValueError, match="max_rows"):
        QueryDefinition.from_mapping({**good, "max_rows": "10"})
    with pytest.raises(ValueError, match="columns"):
        QueryDefinition.from_mapping({**good, "columns": "invoices.invoice_id"})
    with pytest.raises(ValueError, match="unsupported fields"):
        QueryDefinition.from_mapping(
            {
                **good,
                "columns": [
                    {"table": "invoices", "column": "invoice_id", "alias": "id"}
                ],
            }
        )


def database_config() -> dict[str, object]:
    return {
        "allowed_columns": {
            table: list(columns) for table, columns in ALLOWED_COLUMNS.items()
        },
        "queries": [query.to_mapping() for query in query_definitions()],
    }


def test_register_database_config_matches_typed_registration(tmp_path: Path) -> None:
    path = create_database(tmp_path / "contracts.sqlite")
    typed = registered_extension(path)
    configured = SQLiteDatabaseExtension()
    configured.register_database_config(
        source(), path, json.loads(json.dumps(database_config()))
    )
    call = request("invoices.overdue", {"due_before": "2026-02-28", "is_paid": False})

    expected = typed.run_tool(call, RetrievalContext())
    actual = configured.run_tool(call, RetrievalContext())

    assert configured.tools_for_source(source()) == typed.tools_for_source(source())
    assert actual.value["rows"] == expected.value["rows"]
    assert (
        actual.locator.value["query_definition_sha256"]
        == expected.locator.value["query_definition_sha256"]
    )
    typed.close()
    configured.close()


def test_register_database_config_rejects_malformed_configuration(
    tmp_path: Path,
) -> None:
    path = create_database(tmp_path / "contracts.sqlite")
    extension = SQLiteDatabaseExtension()
    good = database_config()
    bad_queries: list[object] = ["invoices.overdue"]
    bad_configs: list[dict[str, object]] = [
        {**good, "sql": "SELECT 1"},
        {"allowed_columns": good["allowed_columns"]},
        {**good, "allowed_columns": ["invoices"]},
        {**good, "queries": []},
        {**good, "queries": bad_queries},
        {**good, "queries": [{**query_definitions()[0].to_mapping(), "sql": "x"}]},
    ]

    for config in bad_configs:
        with pytest.raises(InvalidDatabaseConfigurationError):
            extension.register_database_config(source(), path, config)
    assert extension.tools_for_source(source()) == ()
    extension.close()


def test_database_source_version_tracks_the_file_state(tmp_path: Path) -> None:
    path = create_database(tmp_path / "contracts.sqlite")
    extension = SQLiteDatabaseExtension()

    version = extension.database_source_version(path)

    assert version.startswith("sqlite-state:")
    assert extension.database_source_version(path) == version
    state = path.stat()
    os.utime(path, ns=(state.st_atime_ns, state.st_mtime_ns + 1_000_000_000))
    assert extension.database_source_version(path) != version
    with pytest.raises(DatabaseRegistrationError):
        extension.database_source_version(tmp_path / "missing.sqlite")
    extension.close()


def test_unregister_database_drops_every_revision_of_one_source(
    tmp_path: Path,
) -> None:
    path = create_database(tmp_path / "contracts.sqlite")
    extension = registered_extension(path)
    extension.register_database(
        source(source_id="other"),
        path,
        allowed_columns=ALLOWED_COLUMNS,
        queries=query_definitions(),
    )

    assert extension.unregister_database(source().source_id) == 1
    assert extension.unregister_database(source().source_id) == 0

    assert extension.tools_for_source(source()) == ()
    assert len(extension.tools_for_source(source(source_id="other"))) == 1
    extension.close()


def request(query_id: str, filters: dict[str, object], **parameters: object) -> ToolRequest:
    return ToolRequest(
        QUERY_TOOL_ID,
        {
            "source_id": "demo-contracts",
            "source_version": "sqlite-live:fixture-v1",
            "query_id": query_id,
            "filters": filters,
            **parameters,
        },
    )


def test_schema_index_contains_only_allowlisted_metadata_and_fk_nodes(
    tmp_path: Path,
) -> None:
    path = create_database(tmp_path / "contracts.sqlite")
    extension = registered_extension(path)

    nodes = tuple(extension.iter_index_records(source(), IndexContext()))
    tables = {node.title: node for node in nodes if node.kind == "database_table"}
    foreign_key = next(node for node in nodes if node.kind == "database_foreign_key")
    columns = {node.title: node for node in nodes if node.kind == "database_column"}

    assert set(tables) == {"contracts", "invoices"}
    assert tables["contracts"].attributes["primary_key_columns"] == ["contract_id"]
    assert "invoices.invoice_id" in columns
    assert foreign_key.attributes["from_table"] == "invoices"
    assert foreign_key.attributes["to_table"] == "contracts"
    assert foreign_key.attributes["target_node_id"] == tables["contracts"].node_id
    assert "INV-1001" not in json.dumps([node.attributes for node in nodes])
    extension.close()


def test_composite_primary_key_ordering_and_foreign_key_join_are_complete(
    tmp_path: Path,
) -> None:
    path = tmp_path / "composite.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE parent (
                parent_a INTEGER,
                parent_b INTEGER,
                label TEXT,
                PRIMARY KEY (parent_a, parent_b)
            );
            CREATE TABLE child (
                child_id INTEGER PRIMARY KEY,
                parent_a INTEGER,
                parent_b INTEGER,
                FOREIGN KEY (parent_a, parent_b)
                    REFERENCES parent (parent_a, parent_b)
            );
            """
        )
    finally:
        connection.close()

    parent_count = QueryDefinition(
        "parent.count",
        "parent",
        (),
        aggregate=AggregateRule("count", None, "parent_count"),
        max_rows=1,
    )
    restricted_extension = SQLiteDatabaseExtension()
    restricted_extension.register_database(
        source(),
        path,
        allowed_columns={"parent": ("parent_a", "label")},
        queries=(parent_count,),
    )
    parent_node = next(
        node
        for node in restricted_extension.iter_index_records(source(), IndexContext())
        if node.kind == "database_table"
    )
    assert parent_node.attributes["primary_key_columns"] == []
    assert parent_node.attributes["primary_key_complete"] is False
    assert parent_node.attributes["primary_key_column_count"] == 2
    restricted_extension.close()

    incomplete_order = QueryDefinition(
        "parent.rows",
        "parent",
        (ColumnRef("parent", "parent_a"),),
        order_by=(ColumnRef("parent", "parent_a"),),
    )
    with pytest.raises(
        InvalidDatabaseConfigurationError,
        match="must order by every joined table primary key",
    ):
        SQLiteDatabaseExtension().register_database(
            source(),
            path,
            allowed_columns={"parent": ("parent_a", "label")},
            queries=(incomplete_order,),
        )

    composite_join = QueryDefinition(
        "child.count",
        "child",
        (),
        joins=(
            JoinRule(
                ColumnRef("child", "parent_a"),
                ColumnRef("parent", "parent_a"),
            ),
        ),
        aggregate=AggregateRule("count", None, "child_count"),
        max_rows=1,
    )
    with pytest.raises(
        InvalidDatabaseConfigurationError,
        match="join is not a declared foreign key",
    ):
        SQLiteDatabaseExtension().register_database(
            source(),
            path,
            allowed_columns={
                "parent": ("parent_a", "parent_b", "label"),
                "child": ("child_id", "parent_a", "parent_b"),
            },
            queries=(composite_join,),
        )


def test_live_filtered_rows_and_joined_sum_return_cited_evidence(
    tmp_path: Path,
) -> None:
    extension = registered_extension(create_database(tmp_path / "contracts.sqlite"))

    rows = extension.run_tool(
        request(
            "invoices.overdue",
            {"due_before": "2026-02-28", "is_paid": False},
            limit=2,
        ),
        RetrievalContext(),
    )
    total = extension.run_tool(
        request(
            "invoices.overdue_total_by_contract",
            {"due_before": "2026-02-28", "is_paid": False},
        ),
        RetrievalContext(),
    )
    count = extension.run_tool(
        request(
            "invoices.overdue_count",
            {"due_before": "2026-02-28", "is_paid": False},
        ),
        RetrievalContext(),
    )

    assert rows.source_id == source().source_id
    assert rows.source_version == source().version
    assert rows.operation == QUERY_TOOL_ID
    assert rows.evidence_kind is EvidenceKind.OBSERVED
    assert rows.locator.value["query_id"] == "invoices.overdue"
    assert rows.locator.value["query_definition_sha256"]
    assert rows.locator.value["database_file_state_sha256"]
    assert rows.value["filters"] == {
        "due_before": "2026-02-28",
        "is_paid": False,
    }
    assert rows.value["effective_limit"] == 2
    assert rows.value["query_definition_sha256"] == rows.locator.value[
        "query_definition_sha256"
    ]
    assert rows.value["row_count"] == 2
    assert rows.value["truncated"] is True
    assert [
        row["invoices__invoice_id"] for row in rows.value["rows"]
    ] == ["INV-1001", "INV-1002"]
    assert rows.locator.value["read_timestamp_utc"].endswith("Z")

    assert total.evidence_kind is EvidenceKind.CALCULATED
    assert total.unit == "USD cents"
    assert total.value["rows"] == [
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
    assert count.value["rows"] == [{"invoice_count": 3}]
    assert count.evidence_kind is EvidenceKind.CALCULATED
    extension.close()


def test_raw_sql_is_rejected_and_filter_values_are_bound(tmp_path: Path) -> None:
    path = create_database(tmp_path / "contracts.sqlite")
    extension = registered_extension(path)

    with pytest.raises(InvalidDatabaseToolRequestError, match="only limit is optional"):
        extension.run_tool(
            request(
                "invoices.by_reference",
                {"reference": "NW-2026-01"},
                sql="DROP TABLE invoices",
            ),
            RetrievalContext(),
        )

    evidence = extension.run_tool(
        request(
            "invoices.by_reference",
            {"reference": "' OR 1=1 --"},
        ),
        RetrievalContext(),
    )
    assert evidence.value["rows"] == []
    tenant_b = extension.run_tool(
        request(
            "invoices.by_reference",
            {"reference": "CT-2026-01"},
        ),
        RetrievalContext(),
    )
    assert tenant_b.value["rows"] == []
    with pytest.raises(InvalidDatabaseToolRequestError, match="filters mismatch"):
        extension.run_tool(
            request(
                "invoices.by_reference",
                {"reference": "CT-2026-01", "invoice_tenant": "tenant-b"},
            ),
            RetrievalContext(),
        )

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 6
    extension.close()


def test_read_only_connection_rejects_dml_and_ddl(tmp_path: Path) -> None:
    connection = open_read_only(create_database(tmp_path / "contracts.sqlite"))
    try:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("UPDATE invoices SET paid = 1")
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DROP TABLE invoices")
    finally:
        connection.close()


def test_unauthorized_columns_and_non_foreign_key_joins_are_rejected(
    tmp_path: Path,
) -> None:
    path = create_database(tmp_path / "contracts.sqlite")
    unauthorized_query = QueryDefinition(
        "invoices.private",
        "invoices",
        (ColumnRef("invoices", "reference"),),
        order_by=(ColumnRef("invoices", "reference"),),
    )
    extension = SQLiteDatabaseExtension()
    restricted_columns = {
        table: tuple(column for column in columns if column != "reference")
        for table, columns in ALLOWED_COLUMNS.items()
    }

    with pytest.raises(UnauthorizedDatabaseAccessError, match="non-allowlisted column"):
        extension.register_database(
            source(),
            path,
            allowed_columns=restricted_columns,
            queries=(unauthorized_query,),
        )

    bad_join = QueryDefinition(
        "invoices.bad_join",
        "invoices",
        (ColumnRef("invoices", "invoice_id"),),
        joins=(
            JoinRule(
                ColumnRef("invoices", "contract_id"),
                ColumnRef("contracts", "currency"),
            ),
        ),
        order_by=(ColumnRef("invoices", "invoice_id"),),
    )
    with pytest.raises(
        InvalidDatabaseConfigurationError, match="not a declared foreign key"
    ):
        extension.register_database(
            source(),
            path,
            allowed_columns=ALLOWED_COLUMNS,
            queries=(bad_join,),
        )
    extension.close()


def test_unknown_filters_excessive_limits_and_expired_deadlines_fail(
    tmp_path: Path,
) -> None:
    extension = registered_extension(create_database(tmp_path / "contracts.sqlite"))

    with pytest.raises(InvalidDatabaseToolRequestError, match="filters mismatch"):
        extension.run_tool(
            request(
                "invoices.by_reference",
                {"other": "NW-2026-01"},
            ),
            RetrievalContext(),
        )
    with pytest.raises(InvalidDatabaseToolRequestError, match="limit"):
        extension.run_tool(
            request(
                "invoices.by_reference",
                {"reference": "NW-2026-01"},
                limit=11,
            ),
            RetrievalContext(),
        )
    with pytest.raises(DatabaseDeadlineExceededError):
        extension.run_tool(
            request(
                "invoices.by_reference",
                {"reference": "NW-2026-01"},
            ),
            RetrievalContext(deadline_monotonic=0),
        )
    extension.close()


def test_null_in_filter_and_aggregate_alias_collisions_are_rejected() -> None:
    item_id = ColumnRef("items", "item_id")
    in_query = QueryDefinition(
        "items.by_id",
        "items",
        (item_id,),
        filters=(FilterRule("ids", item_id, "in"),),
        order_by=(item_id,),
    )
    with pytest.raises(InvalidDatabaseToolRequestError, match="cannot contain null"):
        build_query(in_query, {"ids": [None, 1]}, limit=1)

    with pytest.raises(ValueError, match="aggregate alias collides"):
        QueryDefinition(
            "items.grouped",
            "items",
            (item_id,),
            aggregate=AggregateRule("sum", item_id, "items__item_id"),
            group_by=(item_id,),
        )

    with pytest.raises(ValueError, match="selected column output aliases"):
        QueryDefinition(
            "items.alias_collision",
            "a__b",
            (ColumnRef("a__b", "c"), ColumnRef("a", "b__c")),
            order_by=(ColumnRef("a__b", "c"), ColumnRef("a", "b__c")),
        )


def test_query_filter_values_cannot_exceed_sqlite_parameter_compatibility_limit() -> None:
    filters = tuple(
        FilterRule(
            f"filter_{index}",
            ColumnRef("items", f"value_{index}"),
            "in",
        )
        for index in range(10)
    )
    definition = QueryDefinition(
        "items.bounded",
        "items",
        (ColumnRef("items", "item_id"),),
        filters=filters,
        order_by=(ColumnRef("items", "item_id"),),
    )

    with pytest.raises(InvalidDatabaseToolRequestError, match="at most 900"):
        build_query(
            definition,
            {rule.name: list(range(100)) for rule in filters},
            limit=10,
        )


def test_result_bytes_are_bounded_while_rows_are_read(tmp_path: Path) -> None:
    path = tmp_path / "large-results.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE records (record_id INTEGER PRIMARY KEY, payload TEXT)"
        )
        connection.executemany(
            "INSERT INTO records (record_id, payload) VALUES (?, ?)",
            ((record_id, "x" * 1000) for record_id in range(100)),
        )
        connection.commit()
    finally:
        connection.close()

    record_id = ColumnRef("records", "record_id")
    extension = SQLiteDatabaseExtension()
    extension.register_database(
        source(),
        path,
        allowed_columns={"records": ("record_id", "payload")},
        queries=(
            QueryDefinition(
                "records.all",
                "records",
                (record_id, ColumnRef("records", "payload")),
                order_by=(record_id,),
            ),
        ),
    )
    with pytest.raises(DatabaseResultTooLargeError):
        extension.run_tool(request("records.all", {}), RetrievalContext())
    extension.close()


def test_database_file_changes_require_a_new_source_version(tmp_path: Path) -> None:
    path = create_database(tmp_path / "contracts.sqlite")
    extension = registered_extension(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE invoices SET reference = ? WHERE invoice_id = ?",
            ("NW-2026-99", "INV-1001"),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DatabaseSourceChangedError, match="new source version"):
        extension.run_tool(
            request(
                "invoices.by_reference",
                {"reference": "NW-2026-99"},
            ),
            RetrievalContext(),
        )
    extension.close()


def test_registering_new_revision_keeps_prior_snapshot_available(
    tmp_path: Path,
) -> None:
    original_path = create_database(tmp_path / "original.sqlite")
    revised_path = create_database(tmp_path / "revised.sqlite")
    connection = sqlite3.connect(revised_path)
    try:
        connection.execute(
            "UPDATE invoices SET reference = ? WHERE invoice_id = ?",
            ("NW-2026-99", "INV-1001"),
        )
        connection.commit()
    finally:
        connection.close()

    extension = SQLiteDatabaseExtension()
    extension.register_database(
        source(),
        original_path,
        allowed_columns=ALLOWED_COLUMNS,
        queries=query_definitions(),
    )
    revised_source = source(version="sqlite-live:fixture-v2")
    extension.register_database(
        revised_source,
        revised_path,
        allowed_columns=ALLOWED_COLUMNS,
        queries=query_definitions(),
    )

    old_result = extension.run_tool(
        request(
            "invoices.by_reference",
            {"reference": "NW-2026-01"},
        ),
        RetrievalContext(),
    )
    new_result = extension.run_tool(
        request(
            "invoices.by_reference",
            {"reference": "NW-2026-99"},
            source_version=revised_source.version,
        ),
        RetrievalContext(),
    )
    assert old_result.value["rows"][0]["invoices__reference"] == "NW-2026-01"
    assert new_result.value["rows"][0]["invoices__reference"] == "NW-2026-99"
    extension.close()


def test_virtual_machine_instruction_budget_interrupts_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataindex_ext_databases import sqlite as sqlite_module

    monkeypatch.setattr(sqlite_module, "SQLITE_PROGRESS_INTERVAL", 1)
    monkeypatch.setattr(sqlite_module, "MAX_SQLITE_VM_INSTRUCTIONS", 1)
    extension = registered_extension(create_database(tmp_path / "contracts.sqlite"))

    with pytest.raises(DatabaseExecutionBudgetExceededError):
        extension.run_tool(
            request(
                "invoices.by_reference",
                {"reference": "NW-2026-01"},
            ),
            RetrievalContext(),
        )
    extension.close()


def test_schema_changes_require_a_new_source_version(tmp_path: Path) -> None:
    path = create_database(tmp_path / "contracts.sqlite")
    extension = registered_extension(path)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE invoices ADD COLUMN internal_note TEXT")

    with pytest.raises(DatabaseSchemaChangedError, match="new source version"):
        tuple(extension.iter_index_records(source(), IndexContext()))
    with pytest.raises(DatabaseSchemaChangedError, match="new source version"):
        extension.run_tool(
            request(
                "invoices.by_reference",
                {"reference": "NW-2026-01"},
            ),
            RetrievalContext(),
        )
    extension.close()


def test_installed_entry_point_requires_trust_and_enablement() -> None:
    point = next(
        entry_point
        for entry_point in entry_points(group=ENTRY_POINT_GROUP)
        if entry_point.name == "databases"
    )
    distribution_name = point.dist.name
    distribution_version = point.dist.version
    assert distribution_name == "dataindex-ext-databases"
    assert distribution_version == "0.1.0"

    disabled = ExtensionRegistry(
        trusted_distributions={(distribution_name, distribution_version)}
    )
    discovered = disabled.discover((point,))[0]
    assert discovered.compatible and discovered.trusted and not discovered.enabled
    assert disabled.load_enabled()[0].healthy is None

    enabled = ExtensionRegistry(
        trusted_distributions={(distribution_name, distribution_version)},
        enabled_plugin_ids={"databases"},
    )
    status = enabled.discover((point,))[0]
    assert status.compatible and status.trusted and status.enabled
    assert enabled.load_enabled()[0].healthy
    enabled.loaded_extensions["databases"].close()
