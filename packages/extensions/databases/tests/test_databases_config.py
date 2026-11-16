from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dataindex.models import (
    EvidenceKind,
    EvidenceRecord,
    HealthStatus,
    IndexContext,
    RetrievalContext,
    SourceRef,
    ToolRequest,
)
from dataindex_ext_databases import (
    AggregateRule,
    ClosedDatabaseIndexError,
    ColumnRef,
    DatabaseDeadlineExceededError,
    DatabaseExecutionBudgetExceededError,
    DatabaseIndexError,
    DatabaseNotRegisteredError,
    DatabaseQueryError,
    DatabaseRegistrationError,
    DatabaseResultTooLargeError,
    DatabaseSourceChangedError,
    DatabaseSourceConflictError,
    FilterRule,
    FixedFilterRule,
    InvalidDatabaseConfigurationError,
    InvalidDatabaseToolRequestError,
    JoinRule,
    QueryDefinition,
    SQLiteDatabaseExtension,
    UnauthorizedDatabaseAccessError,
    UnsupportedDatabaseSourceError,
)
from dataindex_ext_databases import plugin as plugin_module
from dataindex_ext_databases import sqlite as sqlite_module
from dataindex_ext_databases.plugin import QUERY_TOOL_ID
from dataindex_ext_databases.queries import build_query
from dataindex_ext_databases.sqlite import (
    NO_VIEWS,
    ViewAccess,
    inspect_view_access,
    open_read_only,
    run_bounded_query,
)

FIXTURE = Path(__file__).parents[1] / "examples" / "fixtures" / "contracts_invoices.sql"
SOURCE_TYPE = "application/vnd.sqlite3"
CANARY = "CANARY-TENANT-7f3a"
HOSTILE = "x'; DROP TABLE invoices; --"
ALLOWED_COLUMNS: dict[str, tuple[str, ...]] = {
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
EVENT_COLUMNS: dict[str, tuple[str, ...]] = {"events": ("event_id", "kind", "amount")}
TENANT_A_INVOICES = ["INV-1001", "INV-1002", "INV-1003", "INV-2001", "INV-2002"]
ALL_INVOICES = [*TENANT_A_INVOICES, "INV-9001"]


def source(source_id: str = "db1", version: str = "v1") -> SourceRef:
    return SourceRef(source_id, SOURCE_TYPE, version)


def inv(column: str) -> ColumnRef:
    return ColumnRef("invoices", column)


def col(table: str, column: str) -> dict[str, str]:
    return {"table": table, "column": column}


def without(table: str, column: str) -> dict[str, tuple[str, ...]]:
    return {
        name: tuple(item for item in columns if (name, item) != (table, column))
        for name, columns in ALLOWED_COLUMNS.items()
    }


RESTRICTED = without("invoices", "reference")


@functools.cache
def fixture_image(tenant: str) -> bytes:
    # Built once in memory: executescript on a file commits (and fsyncs) per statement.
    with contextlib.closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(FIXTURE.read_text(encoding="utf-8"))
        if tenant != "tenant-a":
            for table in ("contracts", "invoices"):
                connection.execute(
                    f"UPDATE {table} SET tenant_id = ? WHERE tenant_id = 'tenant-a'",
                    (tenant,),
                )
            connection.commit()
        return connection.serialize()


def create_database(path: Path, tenant: str = "tenant-a") -> Path:
    path.write_bytes(fixture_image(tenant))
    return path


def create_events_database(path: Path, rows: int = 50_000) -> Path:
    with contextlib.closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "CREATE TABLE events ("
            "event_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, amount INTEGER NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO events (event_id, kind, amount) VALUES (?, 'sale', ?)",
            ((number, number % 97) for number in range(1, rows + 1)),
        )
        connection.commit()
    return path


def change_database(path: Path) -> None:
    with contextlib.closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "UPDATE invoices SET amount_cents = 1 WHERE invoice_id = 'INV-1001'"
        )
        connection.commit()
    state = path.stat()
    os.utime(path, ns=(state.st_atime_ns, state.st_mtime_ns + 1_000_000_000))


def invoice_query(
    query_id: str = "invoices.list",
    *,
    filters: tuple[FilterRule, ...] = (),
    scope: tuple[str, object] | None = None,
    max_rows: int = 100,
    unit: str | None = None,
) -> QueryDefinition:
    fixed = (
        ()
        if scope is None
        else (FixedFilterRule("tenant_scope", inv("tenant_id"), *scope),)
    )
    return QueryDefinition(
        query_id,
        "invoices",
        (inv("invoice_id"), inv("amount_cents")),
        filters=filters,
        source_filters=fixed,
        order_by=(inv("invoice_id"),),
        max_rows=max_rows,
        unit=unit,
    )


def count_query(
    query_id: str = "invoices.count",
    *,
    table: str = "invoices",
    scope: tuple[str, object] | None = None,
) -> QueryDefinition:
    fixed = (
        ()
        if scope is None
        else (FixedFilterRule("tenant_scope", ColumnRef(table, "tenant_id"), *scope),)
    )
    return QueryDefinition(
        query_id,
        table,
        (),
        source_filters=fixed,
        aggregate=AggregateRule("count", None, "n"),
        max_rows=1,
    )


def rich_query() -> QueryDefinition:
    contract_id = ColumnRef("contracts", "contract_id")
    return QueryDefinition(
        "invoices.total_by_contract",
        "invoices",
        (contract_id,),
        filters=(FilterRule("min_due", inv("due_date"), "gte"),),
        source_filters=(
            FixedFilterRule(
                "tenant_scope", inv("tenant_id"), "in", ("tenant-a", "tenant-b")
            ),
            FixedFilterRule("unpaid_only", inv("paid"), "eq", 0),
        ),
        joins=(JoinRule(inv("contract_id"), contract_id, "left"),),
        aggregate=AggregateRule("sum", inv("amount_cents"), "total_cents"),
        group_by=(contract_id,),
        order_by=(contract_id,),
        max_rows=25,
        unit="USD cents",
    )


def scan_query() -> QueryDefinition:
    # "kind" is not indexed, so any filter value forces a full table scan.
    return QueryDefinition(
        "events.total",
        "events",
        (),
        filters=(FilterRule("kind", ColumnRef("events", "kind")),),
        aggregate=AggregateRule("sum", ColumnRef("events", "amount"), "total"),
        max_rows=1,
    )


def lookup_query() -> QueryDefinition:
    event_id = ColumnRef("events", "event_id")
    return QueryDefinition(
        "events.by_id",
        "events",
        (event_id,),
        filters=(FilterRule("event_id", event_id),),
        order_by=(event_id,),
        max_rows=1,
    )


def page_query() -> QueryDefinition:
    event_id = ColumnRef("events", "event_id")
    return QueryDefinition(
        "events.page", "events", (event_id,), order_by=(event_id,), max_rows=100
    )


def config(
    queries: Sequence[QueryDefinition],
    allowed: Mapping[str, Sequence[str]] = ALLOWED_COLUMNS,
) -> dict[str, Any]:
    # The JSON round trip is what a host persisting this configuration would do.
    stored: dict[str, Any] = json.loads(
        json.dumps(
            {
                "allowed_columns": {
                    table: list(columns) for table, columns in allowed.items()
                },
                "queries": [query.to_mapping() for query in queries],
            }
        )
    )
    return stored


def register(
    extension: SQLiteDatabaseExtension,
    path: Path,
    queries: Sequence[QueryDefinition],
    *,
    ref: SourceRef | None = None,
    allowed: Mapping[str, Sequence[str]] = ALLOWED_COLUMNS,
) -> SourceRef:
    ref = ref or source()
    extension.register_database_config(ref, path, config(queries, allowed))
    return ref


def call(
    extension: SQLiteDatabaseExtension,
    query_id: str,
    filters: Mapping[str, object] | None = None,
    *,
    ref: SourceRef | None = None,
    ctx: RetrievalContext | None = None,
    **parameters: Any,
) -> EvidenceRecord:
    ref = ref or source()
    return extension.run_tool(
        ToolRequest(
            QUERY_TOOL_ID,
            {
                "source_id": ref.source_id,
                "source_version": ref.version,
                "query_id": query_id,
                "filters": {} if filters is None else dict(filters),
                **parameters,
            },
        ),
        ctx or RetrievalContext(),
    )


def value_of(evidence: EvidenceRecord) -> dict[str, Any]:
    assert isinstance(evidence.value, dict)
    return evidence.value


def invoice_ids(evidence: EvidenceRecord) -> list[object]:
    return [row["invoices__invoice_id"] for row in value_of(evidence)["rows"]]


def advertised_query_ids(
    extension: SQLiteDatabaseExtension, ref: SourceRef
) -> list[object]:
    (tool,) = extension.tools_for_source(ref)
    properties = tool.input_schema["properties"]
    assert isinstance(properties, dict)
    query_id = properties["query_id"]
    assert isinstance(query_id, dict)
    enum = query_id["enum"]
    assert isinstance(enum, list)
    return enum


@pytest.fixture
def extension() -> Iterator[SQLiteDatabaseExtension]:
    instance = SQLiteDatabaseExtension()
    yield instance
    instance.close()


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return create_database(tmp_path / "contracts.sqlite")


# --- QueryDefinition.to_mapping / from_mapping ---------------------------------


def test_to_mapping_is_the_json_shape_and_never_aliases_the_definition() -> None:
    query = rich_query()
    expected = {
        "query_id": "invoices.total_by_contract",
        "table": "invoices",
        "columns": [col("contracts", "contract_id")],
        "filters": [
            {
                "name": "min_due",
                "column": col("invoices", "due_date"),
                "operator": "gte",
            }
        ],
        "source_filters": [
            {
                "name": "tenant_scope",
                "column": col("invoices", "tenant_id"),
                "operator": "in",
                "value": ["tenant-a", "tenant-b"],
            },
            {
                "name": "unpaid_only",
                "column": col("invoices", "paid"),
                "operator": "eq",
                "value": 0,
            },
        ],
        "joins": [
            {
                "left": col("invoices", "contract_id"),
                "right": col("contracts", "contract_id"),
                "kind": "left",
            }
        ],
        "aggregate": {
            "function": "sum",
            "column": col("invoices", "amount_cents"),
            "alias": "total_cents",
        },
        "group_by": [col("contracts", "contract_id")],
        "order_by": [col("contracts", "contract_id")],
        "max_rows": 25,
        "unit": "USD cents",
    }

    mapping: dict[str, Any] = query.to_mapping()

    assert mapping == expected
    assert json.loads(json.dumps(mapping)) == mapping
    mapping["columns"].clear()
    mapping["source_filters"][0]["value"].append("tenant-z")
    assert query.to_mapping() == expected
    assert QueryDefinition.from_mapping(expected) == query


def test_unaggregated_count_serialises_with_a_null_column() -> None:
    mapping = count_query().to_mapping()

    assert mapping["aggregate"] == {"function": "count", "column": None, "alias": "n"}
    assert QueryDefinition.from_mapping(mapping) == count_query()
    assert invoice_query().to_mapping()["aggregate"] is None


def test_from_mapping_applies_defaults_and_accepts_tuples() -> None:
    minimal = {
        "query_id": "invoices.list",
        "table": "invoices",
        "columns": [col("invoices", "invoice_id")],
        "order_by": [col("invoices", "invoice_id")],
    }

    query = QueryDefinition.from_mapping(minimal)

    assert (query.filters, query.source_filters, query.joins, query.group_by) == (
        (),
        (),
        (),
        (),
    )
    assert query.aggregate is None
    assert query.max_rows == 100
    assert query.unit is None
    from_tuples = QueryDefinition.from_mapping(
        {
            **minimal,
            "columns": (col("invoices", "invoice_id"),),
            "filters": ({"name": "reference", "column": col("invoices", "reference")},),
            "joins": (
                {
                    "left": col("invoices", "contract_id"),
                    "right": col("contracts", "contract_id"),
                },
            ),
        }
    )
    assert from_tuples.filters == (FilterRule("reference", inv("reference"), "eq"),)
    assert from_tuples.joins == (JoinRule(inv("contract_id"), ColumnRef("contracts", "contract_id"), "inner"),)


@pytest.mark.parametrize(
    ("operator", "value"),
    [
        ("eq", "tenant-a"),
        ("eq", 7),
        ("eq", 2.5),
        ("eq", True),
        ("eq", False),
        ("eq", None),
        ("ne", None),
        ("ne", "tenant-b"),
        ("lt", -3),
        ("in", ("tenant-a", "tenant-b")),
        ("in", (1, 2.5, True, "x")),
    ],
)
def test_fixed_filter_values_keep_their_types_through_the_json_mapping(
    operator: str, value: object
) -> None:
    query = QueryDefinition(
        "invoices.list",
        "invoices",
        (inv("invoice_id"),),
        source_filters=(FixedFilterRule("scope", inv("tenant_id"), operator, value),),
        order_by=(inv("invoice_id"),),
    )

    restored = QueryDefinition.from_mapping(json.loads(json.dumps(query.to_mapping())))

    assert restored == query
    restored_value = restored.source_filters[0].value
    assert type(restored_value) is type(value)
    if isinstance(value, tuple):
        assert isinstance(restored_value, tuple)
        assert [type(item) for item in restored_value] == [type(item) for item in value]


def mapping(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "query_id": "invoices.list",
        "table": "invoices",
        "columns": [col("invoices", "invoice_id")],
        "order_by": [col("invoices", "invoice_id")],
    }
    return {**base, **overrides}


SCOPE = {
    "name": "scope",
    "column": col("invoices", "tenant_id"),
    "operator": "eq",
    "value": "tenant-a",
}
JOIN = {
    "left": col("invoices", "contract_id"),
    "right": col("contracts", "contract_id"),
}
COUNT_ALL = {"function": "count", "column": None, "alias": "n"}
REFERENCE_FILTER = {"name": "reference", "column": col("invoices", "reference")}

INVALID_MAPPINGS = [
    pytest.param({"sql": "SELECT 1"}, "unsupported fields: ['sql']", id="unknown-field"),
    pytest.param(
        {"query_id": "Invoices.List"},
        "query_id must be a simple lowercase identifier",
        id="query-id-uppercase",
    ),
    pytest.param(
        {"query_id": "invoices list"},
        "query_id must be a simple lowercase identifier",
        id="query-id-space",
    ),
    pytest.param(
        {"query_id": ""},
        "query_id must be a simple lowercase identifier",
        id="query-id-empty",
    ),
    pytest.param(
        {"query_id": "a" * 65},
        "query_id must be a simple lowercase identifier",
        id="query-id-too-long",
    ),
    pytest.param({"query_id": 7}, "query_id must be a string", id="query-id-number"),
    pytest.param({"table": 7}, "table must be a string", id="table-number"),
    pytest.param({"table": ""}, "query table must be a non-empty string", id="table-empty"),
    pytest.param(
        {"columns": "invoices.invoice_id"}, "columns must be a list", id="columns-text"
    ),
    pytest.param(
        {"columns": [{**col("invoices", "invoice_id"), "alias": "id"}]},
        "columns has unsupported fields",
        id="column-extra-key",
    ),
    pytest.param(
        {"columns": [["invoices", "invoice_id"]]},
        "columns must be an object with string keys",
        id="column-not-an-object",
    ),
    pytest.param(
        {"columns": [{"table": "invoices", "column": 5}]},
        "column must be a string",
        id="column-name-number",
    ),
    pytest.param(
        {"columns": [{"table": "invoices"}]},
        "column must be a string",
        id="column-name-missing",
    ),
    pytest.param(
        {"columns": [col("invoices", "invoice_id")] * 2},
        "selected columns must be unique",
        id="columns-duplicate",
    ),
    pytest.param(
        {"columns": []}, "non-aggregate queries need columns", id="columns-empty"
    ),
    pytest.param(
        {"columns": [col("invoices", f"column_{number}") for number in range(33)]},
        "at most 32 result columns",
        id="columns-too-many",
    ),
    pytest.param(
        {"order_by": []}, "require deterministic order_by", id="order-by-empty"
    ),
    pytest.param(
        {"group_by": [col("invoices", "invoice_id")]},
        "non-aggregate queries need columns and no group_by",
        id="group-by-without-aggregate",
    ),
    pytest.param({"max_rows": 0}, "max_rows must be between 1 and 100", id="max-rows-zero"),
    pytest.param({"max_rows": -1}, "max_rows must be between 1 and 100", id="max-rows-negative"),
    pytest.param({"max_rows": 101}, "max_rows must be between 1 and 100", id="max-rows-101"),
    pytest.param({"max_rows": True}, "max_rows must be an integer", id="max-rows-bool"),
    pytest.param({"max_rows": "10"}, "max_rows must be an integer", id="max-rows-text"),
    pytest.param({"max_rows": 10.0}, "max_rows must be an integer", id="max-rows-float"),
    pytest.param({"max_rows": None}, "max_rows must be an integer", id="max-rows-null"),
    pytest.param(
        {"unit": ""}, "unit must be a non-empty string or None", id="unit-empty"
    ),
    pytest.param(
        {"unit": "  "}, "unit must be a non-empty string or None", id="unit-blank"
    ),
    pytest.param({"unit": 5}, "unit must be a string or null", id="unit-number"),
    pytest.param({"filters": "reference"}, "filters must be a list", id="filters-text"),
    pytest.param(
        {"filters": [{**REFERENCE_FILTER, "operator": "like"}]},
        "unsupported filter operator 'like'",
        id="filter-operator-like",
    ),
    pytest.param(
        {"filters": [{**REFERENCE_FILTER, "operator": None}]},
        "operator must be a string",
        id="filter-operator-null",
    ),
    pytest.param(
        {"filters": [{**REFERENCE_FILTER, "name": "Reference"}]},
        "filter name must be a simple lowercase identifier",
        id="filter-name-uppercase",
    ),
    pytest.param(
        {"filters": [{**REFERENCE_FILTER, "sql": "x"}]},
        "filter has unsupported fields",
        id="filter-extra-key",
    ),
    pytest.param(
        {"filters": [REFERENCE_FILTER, REFERENCE_FILTER]},
        "filters must not contain duplicate rules",
        id="filters-duplicate-rule",
    ),
    pytest.param(
        {
            "filters": [
                REFERENCE_FILTER,
                {"name": "reference", "column": col("invoices", "due_date")},
            ]
        },
        "filter names must be unique",
        id="filters-duplicate-name",
    ),
    pytest.param(
        {
            "filters": [
                {"name": f"filter_{number}", "column": col("invoices", "reference")}
                for number in range(65)
            ]
        },
        "at most 64 filters",
        id="filters-too-many",
    ),
    pytest.param(
        {"source_filters": [{**SCOPE, "operator": "like"}]},
        "unsupported fixed filter operator 'like'",
        id="fixed-operator-like",
    ),
    pytest.param(
        {"source_filters": [{k: v for k, v in SCOPE.items() if k != "operator"}]},
        "operator must be a string",
        id="fixed-operator-missing",
    ),
    pytest.param(
        {"source_filters": [{**SCOPE, "value": {"tenant": "a"}}]},
        "fixed filter value must be a bounded scalar",
        id="fixed-value-object",
    ),
    pytest.param(
        {"source_filters": [{**SCOPE, "value": ["tenant-a"]}]},
        "fixed filter value must be a bounded scalar",
        id="fixed-value-list-for-eq",
    ),
    pytest.param(
        {"source_filters": [{**SCOPE, "value": "x" * 4097}]},
        "fixed filter value must be a bounded scalar",
        id="fixed-value-too-long",
    ),
    pytest.param(
        {"source_filters": [{**SCOPE, "value": 2**63}]},
        "fixed filter value must be a bounded scalar",
        id="fixed-value-integer-overflow",
    ),
    pytest.param(
        {"source_filters": [{**SCOPE, "operator": "in", "value": "tenant-a"}]},
        "fixed IN filters require a bounded scalar tuple",
        id="fixed-in-scalar",
    ),
    pytest.param(
        {"source_filters": [{**SCOPE, "operator": "in", "value": []}]},
        "fixed IN filters require a bounded scalar tuple",
        id="fixed-in-empty",
    ),
    pytest.param(
        {"source_filters": [{**SCOPE, "operator": "in", "value": ["a", None]}]},
        "fixed IN filters require a bounded scalar tuple",
        id="fixed-in-null-member",
    ),
    pytest.param(
        {"source_filters": [{**SCOPE, "operator": "in", "value": list(range(101))}]},
        "fixed IN filters require a bounded scalar tuple",
        id="fixed-in-too-many",
    ),
    pytest.param(
        {"source_filters": [SCOPE, {**SCOPE, "value": "tenant-b"}]},
        "source filter names must be unique",
        id="fixed-duplicate-name",
    ),
    pytest.param(
        {"aggregate": {**COUNT_ALL, "function": "avg"}},
        "aggregate function must be 'count' or 'sum'",
        id="aggregate-avg",
    ),
    pytest.param(
        {"aggregate": {**COUNT_ALL, "function": "count(*)"}},
        "aggregate function must be 'count' or 'sum'",
        id="aggregate-expression",
    ),
    pytest.param(
        {"aggregate": {"function": "sum", "column": None, "alias": "total"}},
        "sum requires a column",
        id="aggregate-sum-without-column",
    ),
    pytest.param(
        {"aggregate": {**COUNT_ALL, "alias": "Total Cents"}},
        "aggregate alias must be a simple lowercase identifier",
        id="aggregate-alias",
    ),
    pytest.param(
        {"aggregate": "count"},
        "aggregate must be an object with string keys",
        id="aggregate-text",
    ),
    pytest.param(
        {"aggregate": {**COUNT_ALL, "distinct": True}},
        "aggregate has unsupported fields",
        id="aggregate-extra-key",
    ),
    pytest.param(
        {"aggregate": COUNT_ALL},
        "aggregate query columns must exactly match group_by",
        id="aggregate-columns-without-group-by",
    ),
    pytest.param(
        {
            "aggregate": {**COUNT_ALL, "alias": "invoices__invoice_id"},
            "group_by": [col("invoices", "invoice_id")],
        },
        "aggregate alias collides with a selected column",
        id="aggregate-alias-collision",
    ),
    pytest.param(
        {"aggregate": COUNT_ALL, "columns": [], "order_by": [col("invoices", "invoice_id")]},
        "ungrouped aggregate queries cannot declare order_by",
        id="aggregate-ungrouped-order-by",
    ),
    pytest.param(
        {
            "aggregate": COUNT_ALL,
            "columns": [col("invoices", "invoice_id")],
            "group_by": [col("invoices", "invoice_id")],
            "order_by": [col("invoices", "reference")],
        },
        "aggregate order_by columns must be grouped",
        id="aggregate-order-by-not-grouped",
    ),
    pytest.param(
        {"joins": [{**JOIN, "kind": "cross"}]},
        "join kind must be 'inner' or 'left'",
        id="join-cross",
    ),
    pytest.param(
        {"joins": [{**JOIN, "right": col("invoices", "invoice_id")}]},
        "join endpoints must belong to different tables",
        id="join-same-table",
    ),
    pytest.param(
        {"joins": [{"left": col("invoices", "contract_id")}]},
        "join right must be an object with string keys",
        id="join-missing-endpoint",
    ),
    pytest.param(
        {"joins": [JOIN] * 33}, "at most 32 joins", id="joins-too-many"
    ),
]


@pytest.mark.parametrize(("overrides", "message"), INVALID_MAPPINGS)
def test_from_mapping_rejects_invalid_definitions(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        QueryDefinition.from_mapping(mapping(**overrides))


@pytest.mark.parametrize("candidate", [[], "query", None, 7, {1: "x"}])
def test_from_mapping_requires_an_object_with_string_keys(candidate: Any) -> None:
    with pytest.raises(ValueError, match="query definition must be an object"):
        QueryDefinition.from_mapping(candidate)


@pytest.mark.parametrize(("overrides", "message"), INVALID_MAPPINGS)
def test_registration_reports_invalid_definitions_before_touching_the_database(
    extension: SQLiteDatabaseExtension,
    tmp_path: Path,
    overrides: dict[str, object],
    message: str,
) -> None:
    bad = {
        "allowed_columns": {"invoices": ["invoice_id"]},
        "queries": [mapping(**overrides)],
    }

    with pytest.raises(
        InvalidDatabaseConfigurationError,
        match=re.escape(f"invalid query definition: ") + ".*" + re.escape(message),
    ):
        extension.register_database_config(source(), tmp_path / "unused.sqlite", bad)

    assert extension.tools_for_source(source()) == ()


# --- register_database_config -------------------------------------------------


def configuration_shape_cases() -> list[Any]:
    good = config([invoice_query()])
    only = good["queries"][0]
    exact = "database configuration requires exactly allowed_columns and queries"
    allowlist = "allowed_columns must map tables to column names"
    queries = "queries must be a list of 1 to 128 definitions"
    return [
        pytest.param(None, exact, id="not-a-mapping"),
        pytest.param([], exact, id="list"),
        pytest.param({}, exact, id="empty"),
        pytest.param({"allowed_columns": good["allowed_columns"]}, exact, id="no-queries-key"),
        pytest.param({"queries": good["queries"]}, exact, id="no-allowlist-key"),
        pytest.param({**good, "sql": "SELECT 1"}, exact, id="extra-key"),
        pytest.param({**good, "allowed_columns": ["invoices"]}, allowlist, id="allowlist-list"),
        pytest.param({**good, "allowed_columns": "invoices"}, allowlist, id="allowlist-text"),
        pytest.param({**good, "queries": []}, queries, id="no-queries"),
        pytest.param({**good, "queries": only}, queries, id="single-object"),
        pytest.param({**good, "queries": "invoices.list"}, queries, id="queries-text"),
        pytest.param({**good, "queries": [only] * 129}, queries, id="too-many-queries"),
        pytest.param({**good, "queries": ["invoices.list"]}, "invalid query definition", id="query-text"),
        pytest.param({**good, "queries": [None]}, "invalid query definition", id="query-null"),
    ]


@pytest.mark.parametrize(("bad", "message"), configuration_shape_cases())
def test_register_database_config_rejects_malformed_configurations(
    extension: SQLiteDatabaseExtension, database: Path, bad: Any, message: str
) -> None:
    with pytest.raises(InvalidDatabaseConfigurationError, match=re.escape(message)):
        extension.register_database_config(source(), database, bad)

    assert extension.tools_for_source(source()) == ()


@pytest.mark.parametrize(
    ("allowed", "message"),
    [
        pytest.param({}, "at least one table", id="empty"),
        pytest.param({"invoices": "invoice_id"}, "must be a sequence of names", id="text-columns"),
        pytest.param({"invoices": []}, "non-empty strings", id="no-columns"),
        pytest.param({"invoices": [7]}, "non-empty strings", id="non-text-column"),
        pytest.param({"invoices": ["invoice_id"] * 2}, "must be unique", id="duplicate-columns"),
        pytest.param({"": ["invoice_id"]}, "non-empty strings", id="empty-table"),
        pytest.param({"sqlite_master": ["name"]}, "internal tables", id="sqlite-internal"),
        pytest.param({"SQLite_Sequence": ["name"]}, "internal tables", id="sqlite-internal-case"),
    ],
)
def test_register_database_config_rejects_malformed_allowlists(
    extension: SQLiteDatabaseExtension,
    database: Path,
    allowed: dict[str, Any],
    message: str,
) -> None:
    bad = {"allowed_columns": allowed, "queries": config([invoice_query()])["queries"]}

    with pytest.raises(InvalidDatabaseConfigurationError, match=re.escape(message)):
        extension.register_database_config(source(), database, bad)

    assert extension.tools_for_source(source()) == ()


def test_allowlist_entries_must_exist_in_the_database(
    extension: SQLiteDatabaseExtension, database: Path
) -> None:
    queries = [invoice_query()]
    for allowed in (
        {**ALLOWED_COLUMNS, "missing_table": ("a",)},
        {**ALLOWED_COLUMNS, "invoices": (*ALLOWED_COLUMNS["invoices"], "no_such_column")},
        {**ALLOWED_COLUMNS, 'inv"; DROP TABLE invoices; --': ("a",)},
    ):
        with pytest.raises(DatabaseRegistrationError, match="could not inspect"):
            register(extension, database, queries, allowed=allowed)

    assert extension.tools_for_source(source()) == ()


def test_registration_requires_a_supported_source_and_a_real_database_file(
    extension: SQLiteDatabaseExtension, database: Path, tmp_path: Path
) -> None:
    queries = [invoice_query()]
    not_a_database = tmp_path / "notes.txt"
    not_a_database.write_text("this is not a database " * 20, encoding="utf-8")

    with pytest.raises(UnsupportedDatabaseSourceError, match="unsupported"):
        register(extension, database, queries, ref=SourceRef("db1", "text/csv", "v1"))
    with pytest.raises(InvalidDatabaseConfigurationError, match="at most 256"):
        register(extension, database, queries, ref=source("x" * 257))
    with pytest.raises(InvalidDatabaseConfigurationError, match="at most 256"):
        register(extension, database, queries, ref=source(version="v" * 257))
    for path in (tmp_path / "missing.sqlite", tmp_path):
        with pytest.raises(DatabaseRegistrationError, match="existing local file|a file"):
            register(extension, path, queries)
    with pytest.raises(DatabaseRegistrationError, match="could not inspect"):
        register(extension, not_a_database, queries)

    assert extension.tools_for_source(source()) == ()
    register(extension, database, queries, ref=SourceRef("db1", "application/x-sqlite3", "v1"))


def test_failed_registrations_never_disturb_registered_sources(
    extension: SQLiteDatabaseExtension, database: Path
) -> None:
    good = register(extension, database, [invoice_query("invoices.list", max_rows=3)], ref=source("good"))
    tools_before = extension.tools_for_source(good)
    forbidden = QueryDefinition(
        "invoices.private",
        "invoices",
        (inv("invoice_id"), inv("reference")),
        order_by=(inv("invoice_id"),),
    )
    failures: list[tuple[type[Exception], dict[str, Any]]] = [
        (InvalidDatabaseConfigurationError, {**config([invoice_query()]), "queries": []}),
        (
            InvalidDatabaseConfigurationError,
            {**config([invoice_query()]), "queries": [mapping(sql="DROP TABLE invoices")]},
        ),
        (UnauthorizedDatabaseAccessError, config([forbidden], allowed=RESTRICTED)),
        (
            DatabaseRegistrationError,
            config([invoice_query()], allowed={**ALLOWED_COLUMNS, "missing_table": ("a",)}),
        ),
    ]

    for error, bad in failures:
        with pytest.raises(error):
            extension.register_database_config(source("bad"), database, bad)
        assert extension.tools_for_source(source("bad")) == ()

    assert extension.tools_for_source(good) == tools_before
    assert value_of(call(extension, "invoices.list", ref=good))["effective_limit"] == 3
    assert extension.unregister_database("bad") == 0


@pytest.mark.parametrize("position", [
    "selected column",
    "caller filter",
    "fixed filter",
    "order by",
    "aggregate column",
    "group by",
])
def test_every_query_column_reference_must_be_allowlisted(
    extension: SQLiteDatabaseExtension, database: Path, position: str
) -> None:
    key, secret = inv("invoice_id"), inv("reference")
    queries = {
        "selected column": QueryDefinition("q.sel", "invoices", (key, secret), order_by=(key,)),
        "caller filter": QueryDefinition(
            "q.filter", "invoices", (key,), filters=(FilterRule("ref", secret),), order_by=(key,)
        ),
        "fixed filter": QueryDefinition(
            "q.fixed",
            "invoices",
            (key,),
            source_filters=(FixedFilterRule("scope", secret, "eq", "x"),),
            order_by=(key,),
        ),
        "order by": QueryDefinition("q.order", "invoices", (key,), order_by=(key, secret)),
        "aggregate column": QueryDefinition(
            "q.count", "invoices", (), aggregate=AggregateRule("count", secret, "n"), max_rows=1
        ),
        "group by": QueryDefinition(
            "q.group",
            "invoices",
            (secret,),
            aggregate=AggregateRule("count", None, "n"),
            group_by=(secret,),
        ),
    }

    with pytest.raises(UnauthorizedDatabaseAccessError, match="non-allowlisted column"):
        register(extension, database, [queries[position]], allowed=RESTRICTED)

    assert extension.tools_for_source(source()) == ()


def test_queries_cannot_name_tables_or_columns_outside_the_allowlist(
    extension: SQLiteDatabaseExtension, database: Path
) -> None:
    only_invoices = {"invoices": ALLOWED_COLUMNS["invoices"]}
    contract_id = ColumnRef("contracts", "contract_id")
    hostile_table = 'invoices" ; DROP TABLE invoices; --'
    hostile = ColumnRef(hostile_table, "invoice_id")
    hostile_column = ColumnRef("invoices", 'invoice_id" FROM invoices; --')
    queries = [
        QueryDefinition("q.other", "contracts", (contract_id,), order_by=(contract_id,)),
        QueryDefinition("q.upper", "INVOICES", (ColumnRef("INVOICES", "invoice_id"),),
                        order_by=(ColumnRef("INVOICES", "invoice_id"),)),
        QueryDefinition("q.table", hostile_table, (hostile,), order_by=(hostile,)),
        QueryDefinition("q.column", "invoices", (hostile_column,), order_by=(hostile_column,)),
        QueryDefinition(
            "q.join",
            "invoices",
            (inv("invoice_id"),),
            joins=(JoinRule(inv("contract_id"), contract_id),),
            order_by=(inv("invoice_id"),),
        ),
    ]

    for query in queries:
        with pytest.raises(UnauthorizedDatabaseAccessError):
            register(extension, database, [query], allowed=only_invoices)

    assert extension.tools_for_source(source()) == ()
    with contextlib.closing(sqlite3.connect(database)) as check:
        assert check.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 6


def test_identifiers_with_quotes_are_quoted_not_interpolated(
    extension: SQLiteDatabaseExtension, tmp_path: Path
) -> None:
    path = tmp_path / "quoted.sqlite"
    with contextlib.closing(sqlite3.connect(path)) as connection:
        connection.execute('CREATE TABLE "we""ird" (id INTEGER PRIMARY KEY, "na""me" TEXT)')
        connection.executemany(
            'INSERT INTO "we""ird" VALUES (?, ?)', [(1, "alpha"), (2, "beta")]
        )
        connection.commit()
    table = 'we"ird'
    row_id, name = ColumnRef(table, "id"), ColumnRef(table, 'na"me')
    register(
        extension,
        path,
        [
            QueryDefinition(
                "weird.list", table, (row_id, name), filters=(FilterRule("name", name),),
                order_by=(row_id,),
            )
        ],
        allowed={table: ("id", 'na"me')},
    )

    evidence = call(extension, "weird.list", {"name": "beta"})

    assert value_of(evidence)["rows"] == [{'we"ird__id': 2, 'we"ird__na"me': "beta"}]


def test_a_rich_configuration_executes_end_to_end(
    extension: SQLiteDatabaseExtension, database: Path
) -> None:
    register(extension, database, [rich_query()])

    everything = call(extension, "invoices.total_by_contract", {"min_due": "2026-01-01"})
    later = call(extension, "invoices.total_by_contract", {"min_due": "2026-02-15"})

    assert value_of(everything)["rows"] == [
        {"contracts__contract_id": 1, "total_cents": 220000},
        {"contracts__contract_id": 2, "total_cents": 90000},
        {"contracts__contract_id": 3, "total_cents": 9000000},
    ]
    assert value_of(later)["rows"] == [{"contracts__contract_id": 1, "total_cents": 70000}]
    assert everything.evidence_kind is EvidenceKind.CALCULATED
    assert everything.unit == "USD cents"


def test_registering_the_same_key_twice_is_a_no_op_only_for_identical_configuration(
    extension: SQLiteDatabaseExtension, database: Path, tmp_path: Path
) -> None:
    original = [invoice_query("invoices.list", max_rows=3)]
    ref = register(extension, database, original)
    tools_before = extension.tools_for_source(ref)

    register(extension, database, original)
    assert extension.tools_for_source(ref) == tools_before

    conflicting: dict[str, Callable[[], object]] = {
        "changed query": lambda: register(
            extension, database, [invoice_query("invoices.list", max_rows=4)]
        ),
        "changed allowlist": lambda: register(extension, database, original, allowed=RESTRICTED),
        "another file": lambda: register(
            extension, create_database(tmp_path / "copy.sqlite"), original
        ),
    }
    for label, attempt in conflicting.items():
        with pytest.raises(
            DatabaseSourceConflictError, match="different database configuration"
        ):
            attempt()
        assert extension.tools_for_source(ref) == tools_before, label

    assert value_of(call(extension, "invoices.list"))["effective_limit"] == 3


def test_a_source_type_cannot_change_between_revisions_or_be_misreported(
    extension: SQLiteDatabaseExtension, database: Path
) -> None:
    ref = register(extension, database, [invoice_query()])
    other_type = SourceRef(ref.source_id, "application/x-sqlite3", "v2")

    with pytest.raises(DatabaseSourceConflictError, match="source type cannot change"):
        register(extension, database, [invoice_query()], ref=other_type)
    with pytest.raises(DatabaseSourceConflictError, match="source type does not match"):
        extension.tools_for_source(SourceRef(ref.source_id, "application/x-sqlite3", ref.version))

    assert len(extension.tools_for_source(ref)) == 1


def test_registration_capacity_is_bounded_and_freed_by_unregistering(
    extension: SQLiteDatabaseExtension,
    database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(plugin_module, "MAX_REGISTERED_SOURCES", 3)
    queries = [invoice_query()]
    register(extension, database, queries, ref=source("db1", "v1"))
    register(extension, database, queries, ref=source("db1", "v2"))
    register(extension, database, queries, ref=source("db2", "v1"))

    with pytest.raises(InvalidDatabaseConfigurationError, match="at most 3 source revisions"):
        register(extension, database, queries, ref=source("db3"))
    register(extension, database, queries, ref=source("db2", "v1"))

    assert extension.unregister_database("db1") == 2
    register(extension, database, queries, ref=source("db3"))
    register(extension, database, queries, ref=source("db4"))
    with pytest.raises(InvalidDatabaseConfigurationError, match="at most 3 source revisions"):
        register(extension, database, queries, ref=source("db5"))


# --- tool schema and schema index ---------------------------------------------


def test_source_tool_schema_is_strict_and_lists_only_that_sources_queries(
    extension: SQLiteDatabaseExtension, database: Path
) -> None:
    ref = register(
        extension,
        database,
        [
            invoice_query(
                "invoices.list",
                filters=(FilterRule("min_amount", inv("amount_cents"), "gte"),),
                unit="USD cents",
            ),
            count_query("invoices.count", scope=("eq", "tenant-a")),
        ],
    )

    (tool,) = extension.tools_for_source(ref)
    schema = tool.input_schema
    properties = schema["properties"]
    assert isinstance(properties, dict)

    assert (tool.tool_id, tool.capability, tool.maximum_results) == (
        "databases.query",
        "database.query",
        100,
    )
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["source_id", "source_version", "query_id", "filters"]
    assert set(properties) == {"source_id", "source_version", "query_id", "filters", "limit"}
    assert properties["limit"] == {"type": "integer", "minimum": 1, "maximum": 100}
    assert advertised_query_ids(extension, ref) == ["invoices.count", "invoices.list"]
    description = str(schema["description"])
    assert "min_amount (gte invoices.amount_cents)" in description
    assert "unit: USD cents" in description
    assert "COUNT(*) AS n" in description
    generic = extension.tools()[0].input_schema["properties"]
    assert isinstance(generic, dict) and "enum" not in generic["query_id"]
    assert extension.tools_for_source(source("db2")) == ()
    assert extension.tools_for_source(source(version="v2")) == ()


def test_schema_index_follows_the_allowlist_and_never_holds_row_values(
    extension: SQLiteDatabaseExtension, database: Path
) -> None:
    ref = register(extension, database, [invoice_query()], allowed=RESTRICTED)
    other = register(extension, database, [invoice_query()], ref=source("db2"), allowed=RESTRICTED)

    nodes = tuple(extension.iter_index_records(ref, IndexContext()))
    text = repr(nodes)

    assert len(nodes) == 14
    assert sorted({node.kind for node in nodes}) == [
        "database",
        "database_column",
        "database_foreign_key",
        "database_table",
    ]
    assert "invoices.reference" not in {node.title for node in nodes}
    assert "reference" not in text
    for row_value in ("INV-1001", "Northwind", "tenant-a", "125000", "NW-2026-01"):
        assert row_value not in text
    assert all((node.source_id, node.source_version) == ("db1", "v1") for node in nodes)
    assert all(node.summary is None for node in nodes)
    assert len({node.node_id for node in nodes}) == len(nodes)
    assert [node.node_id for node in extension.iter_index_records(ref, IndexContext())] == [
        node.node_id for node in nodes
    ]
    assert {node.node_id for node in extension.iter_index_records(other, IndexContext())}.isdisjoint(
        node.node_id for node in nodes
    )


# --- fixed source filters -----------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        pytest.param(("eq", "tenant-a"), TENANT_A_INVOICES, id="eq-a"),
        pytest.param(("eq", "tenant-b"), ["INV-9001"], id="eq-b"),
        pytest.param(("ne", "tenant-a"), ["INV-9001"], id="ne-a"),
        pytest.param(("in", ("tenant-a", "tenant-b")), ALL_INVOICES, id="in-both"),
        pytest.param(("in", ("tenant-b", "tenant-z")), ["INV-9001"], id="in-one-of-two"),
        pytest.param(("eq", "tenant-z"), [], id="eq-unknown"),
        pytest.param(("eq", None), [], id="eq-null"),
        pytest.param(("ne", None), ALL_INVOICES, id="ne-null"),
        pytest.param(("lt", "tenant-b"), TENANT_A_INVOICES, id="lt-b"),
        pytest.param(("gte", "tenant-b"), ["INV-9001"], id="gte-b"),
    ],
)
def test_fixed_source_filters_scope_every_result(
    extension: SQLiteDatabaseExtension,
    database: Path,
    scope: tuple[str, object],
    expected: list[str],
) -> None:
    register(extension, database, [invoice_query(scope=scope)])

    assert invoice_ids(call(extension, "invoices.list")) == expected


def test_callers_cannot_widen_override_or_probe_the_fixed_scope(
    extension: SQLiteDatabaseExtension, database: Path
) -> None:
    register(
        extension,
        database,
        [
            invoice_query(
                "invoices.scoped",
                filters=(FilterRule("tenant", inv("tenant_id")),),
                scope=("eq", "tenant-a"),
            )
        ],
    )

    assert invoice_ids(call(extension, "invoices.scoped", {"tenant": "tenant-a"})) == TENANT_A_INVOICES
    assert invoice_ids(call(extension, "invoices.scoped", {"tenant": "tenant-b"})) == []
    for filters, fragment in (
        ({"tenant": "tenant-b", "tenant_scope": "tenant-b"}, "unknown=['tenant_scope']"),
        ({}, "missing=['tenant']"),
    ):
        with pytest.raises(InvalidDatabaseToolRequestError, match="filters mismatch") as raised:
            call(extension, "invoices.scoped", filters)
        assert fragment in str(raised.value)
        assert "tenant-a" not in str(raised.value)


def test_fixed_filter_values_never_reach_anything_model_visible(
    extension: SQLiteDatabaseExtension, tmp_path: Path
) -> None:
    database = create_database(tmp_path / "canary.sqlite", tenant=CANARY)
    ref = register(
        extension,
        database,
        [
            invoice_query(
                "invoices.scoped",
                filters=(FilterRule("min_amount", inv("amount_cents"), "gte"),),
                scope=("eq", CANARY),
                unit="USD cents",
            ),
            invoice_query("invoices.either", scope=("in", (CANARY, "tenant-b"))),
            count_query("invoices.count", scope=("eq", CANARY)),
        ],
    )
    scoped = call(extension, "invoices.scoped", {"min_amount": 0})
    either = call(extension, "invoices.either")
    counted = call(extension, "invoices.count")
    errors: list[str] = []
    for attempt in (
        lambda: call(extension, "invoices.scoped", {"min_amount": 0, "tenant_scope": "x"}),
        lambda: call(extension, "invoices.scoped", {"min_amount": 0}, limit=0),
        lambda: call(extension, "invoices.unknown"),
    ):
        with pytest.raises(InvalidDatabaseToolRequestError) as raised:
            attempt()
        errors.append(str(raised.value))

    surfaces = {
        "tool schema": json.dumps(extension.tools_for_source(ref)[0].input_schema),
        "tool specs": repr(extension.tools_for_source(ref)) + repr(extension.tools()),
        "index records": repr(tuple(extension.iter_index_records(ref, IndexContext()))),
        "scoped evidence": repr(scoped),
        "either evidence": repr(either),
        "count evidence": repr(counted),
        "errors": " ".join(errors),
    }
    for name, text in surfaces.items():
        assert CANARY not in text, name
    # The scope is really applied, so the absence above is not vacuous.
    assert invoice_ids(scoped) == TENANT_A_INVOICES
    assert invoice_ids(either) == ALL_INVOICES
    assert value_of(counted)["rows"] == [{"n": 5}]


def test_the_definition_digest_binds_the_fixed_scope_without_revealing_it(
    extension: SQLiteDatabaseExtension, database: Path
) -> None:
    for ref, tenant in ((source("db-a"), "tenant-a"), (source("db-b"), "tenant-b")):
        register(extension, database, [invoice_query(scope=("eq", tenant))], ref=ref)

    first = value_of(call(extension, "invoices.list", ref=source("db-a")))
    second = value_of(call(extension, "invoices.list", ref=source("db-b")))
    repeat = value_of(call(extension, "invoices.list", ref=source("db-a")))

    assert first["query_definition_sha256"] != second["query_definition_sha256"]
    assert first["query_definition_sha256"] == repeat["query_definition_sha256"]
    assert re.fullmatch(r"[0-9a-f]{64}", first["query_definition_sha256"])
    assert "tenant-a" not in json.dumps(first["filters"]) + first["query_definition_sha256"]


# --- bound parameters and read-only access ------------------------------------


def test_caller_and_fixed_values_are_bound_parameters_never_sql_text() -> None:
    query = QueryDefinition(
        "invoices.search",
        "invoices",
        (inv("invoice_id"),),
        filters=(
            FilterRule("reference", inv("reference")),
            FilterRule("ids", inv("invoice_id"), "in"),
        ),
        source_filters=(FixedFilterRule("scope", inv("tenant_id"), "in", (HOSTILE, "tenant-a")),),
        order_by=(inv("invoice_id"),),
        max_rows=10,
    )

    statement, parameters = build_query(query, {"reference": HOSTILE, "ids": [HOSTILE, "b"]}, 3)

    assert HOSTILE not in statement and "DROP" not in statement
    assert statement.count("?") == len(parameters)
    assert 'IN (?,?)' in statement and statement.endswith("LIMIT ?")
    assert parameters == (HOSTILE, HOSTILE, "b", HOSTILE, "tenant-a", 4)


def test_hostile_filter_values_are_inert_data(
    extension: SQLiteDatabaseExtension, database: Path
) -> None:
    register(
        extension,
        database,
        [
            invoice_query(
                "invoices.search",
                filters=(
                    FilterRule("reference", inv("reference")),
                    FilterRule("ids", inv("invoice_id"), "in"),
                ),
            )
        ],
    )

    nothing = call(extension, "invoices.search", {"reference": HOSTILE, "ids": ["INV-1001"]})
    one = call(extension, "invoices.search", {"reference": "NW-2026-01", "ids": [HOSTILE, "INV-1001"]})

    assert value_of(nothing)["rows"] == []
    assert invoice_ids(one) == ["INV-1001"]
    assert value_of(one)["filters"] == {"reference": "NW-2026-01", "ids": [HOSTILE, "INV-1001"]}
    with contextlib.closing(sqlite3.connect(database)) as check:
        assert check.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 6


@pytest.mark.parametrize(
    ("filters", "message"),
    [
        pytest.param({"reference": ["a"], "ids": ["x"]}, "must be a scalar value", id="list-for-eq"),
        pytest.param({"reference": {"a": 1}, "ids": ["x"]}, "must be a scalar value", id="object-for-eq"),
        pytest.param({"reference": 2**63, "ids": ["x"]}, "must be a scalar value", id="integer-overflow"),
        pytest.param({"reference": "x" * 4097, "ids": ["x"]}, "must be a scalar value", id="text-too-long"),
        pytest.param({"reference": "a", "ids": []}, "non-empty list of at most 100", id="empty-in"),
        pytest.param({"reference": "a", "ids": "INV-1001"}, "non-empty list of at most 100", id="text-for-in"),
        pytest.param({"reference": "a", "ids": ["x"] * 101}, "non-empty list of at most 100", id="in-too-long"),
        pytest.param({"reference": "a", "ids": [None]}, "cannot contain null", id="null-in-member"),
        pytest.param({"reference": "a", "ids": [[1]]}, "non-scalar value", id="nested-in-member"),
    ],
)
def test_caller_filter_values_are_bounded_scalars(
    extension: SQLiteDatabaseExtension,
    database: Path,
    filters: dict[str, object],
    message: str,
) -> None:
    register(
        extension,
        database,
        [
            invoice_query(
                "invoices.search",
                filters=(
                    FilterRule("reference", inv("reference")),
                    FilterRule("ids", inv("invoice_id"), "in"),
                ),
            )
        ],
    )

    with pytest.raises(InvalidDatabaseToolRequestError, match=re.escape(message)):
        call(extension, "invoices.search", filters)

    assert value_of(call(extension, "invoices.search", {"reference": None, "ids": ["x"]}))["rows"] == []


def test_connections_cannot_write_even_when_query_only_is_switched_off(database: Path) -> None:
    connection = open_read_only(database)
    try:
        connection.execute("PRAGMA query_only = OFF")
        for statement in (
            "INSERT INTO invoices (invoice_id, contract_id, amount_cents, due_date, paid,"
            " tenant_id, reference) VALUES ('x', 1, 1, 'd', 0, 't', 'r')",
            "UPDATE invoices SET paid = 1",
            "DELETE FROM invoices",
            "DROP TABLE invoices",
            "CREATE TABLE t (a)",
            "VACUUM",
        ):
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                connection.execute(statement)
        assert connection.execute("PRAGMA trusted_schema").fetchone()[0] == 0
    finally:
        connection.close()

    with contextlib.closing(sqlite3.connect(database)) as check:
        assert check.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 6


def test_opening_a_missing_database_read_only_does_not_create_it(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite"

    with pytest.raises(sqlite3.OperationalError):
        open_read_only(missing)

    assert not missing.exists()


def test_queries_leave_the_database_file_and_its_directory_untouched(
    extension: SQLiteDatabaseExtension, database: Path
) -> None:
    register(extension, database, [invoice_query(scope=("eq", "tenant-a")), count_query(scope=("eq", "tenant-a"))])
    state = (database.read_bytes(), database.stat().st_mtime_ns)
    names = sorted(path.name for path in database.parent.iterdir())

    call(extension, "invoices.list")
    call(extension, "invoices.count")
    tuple(extension.iter_index_records(source(), IndexContext()))
    with pytest.raises(InvalidDatabaseToolRequestError):
        call(extension, "invoices.list", {"unexpected": 1})
    extension.unregister_database("db1")

    assert (database.read_bytes(), database.stat().st_mtime_ns) == state
    assert sorted(path.name for path in database.parent.iterdir()) == names


# --- authorizer ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "rows"),
    [
        pytest.param(count_query("invoices.count"), [{"n": 6}], id="all-invoices"),
        pytest.param(
            count_query("contracts.count", table="contracts"), [{"n": 3}], id="all-contracts"
        ),
        pytest.param(
            count_query("invoices.count", scope=("eq", "tenant-a")), [{"n": 5}], id="fixed-scope"
        ),
        pytest.param(
            QueryDefinition(
                "invoices.per_contract",
                "invoices",
                (inv("contract_id"),),
                aggregate=AggregateRule("count", None, "n"),
                group_by=(inv("contract_id"),),
                max_rows=10,
            ),
            [
                {"invoices__contract_id": 1, "n": 3},
                {"invoices__contract_id": 2, "n": 2},
                {"invoices__contract_id": 3, "n": 1},
            ],
            id="grouped",
        ),
        pytest.param(
            QueryDefinition(
                "invoices.joined_count",
                "invoices",
                (),
                joins=(JoinRule(inv("contract_id"), ColumnRef("contracts", "contract_id")),),
                aggregate=AggregateRule("count", None, "n"),
                max_rows=1,
            ),
            [{"n": 6}],
            id="joined",
        ),
        pytest.param(
            QueryDefinition(
                "invoices.count_column",
                "invoices",
                (),
                aggregate=AggregateRule("count", inv("reference"), "n"),
                max_rows=1,
            ),
            [{"n": 6}],
            id="count-column",
        ),
    ],
)
def test_count_aggregates_run_with_and_without_filters(
    extension: SQLiteDatabaseExtension,
    database: Path,
    query: QueryDefinition,
    rows: list[dict[str, object]],
) -> None:
    register(extension, database, [query])

    evidence = call(extension, query.query_id)

    assert value_of(evidence)["rows"] == rows
    assert evidence.evidence_kind is EvidenceKind.CALCULATED


@pytest.mark.parametrize(
    ("query", "rows"),
    [
        pytest.param(
            QueryDefinition(
                "contracts.ids",
                "contracts",
                (ColumnRef("contracts", "contract_id"),),
                order_by=(ColumnRef("contracts", "contract_id"),),
                max_rows=10,
            ),
            [{"contracts__contract_id": number} for number in (1, 2, 3)],
            id="integer-key-only",
        ),
        pytest.param(
            QueryDefinition(
                "invoices.contract_keys",
                "invoices",
                (inv("invoice_id"), ColumnRef("contracts", "contract_id")),
                joins=(JoinRule(inv("contract_id"), ColumnRef("contracts", "contract_id")),),
                order_by=(inv("invoice_id"), ColumnRef("contracts", "contract_id")),
                max_rows=10,
            ),
            [
                {"invoices__invoice_id": "INV-1001", "contracts__contract_id": 1},
                {"invoices__invoice_id": "INV-1002", "contracts__contract_id": 1},
                {"invoices__invoice_id": "INV-1003", "contracts__contract_id": 1},
                {"invoices__invoice_id": "INV-2001", "contracts__contract_id": 2},
                {"invoices__invoice_id": "INV-2002", "contracts__contract_id": 2},
                {"invoices__invoice_id": "INV-9001", "contracts__contract_id": 3},
            ],
            id="joined-table-contributes-only-its-key",
        ),
    ],
)
def test_queries_whose_result_reads_only_a_tables_integer_key_run(
    extension: SQLiteDatabaseExtension,
    database: Path,
    query: QueryDefinition,
    rows: list[dict[str, object]],
) -> None:
    # SQLite reports these as a table read with an empty column name, not as a column read.
    register(extension, database, [query])

    assert value_of(call(extension, query.query_id))["rows"] == rows


RAW_ALLOWLIST = {
    table: frozenset(columns) for table, columns in RESTRICTED.items()
}
KEYLESS_ALLOWLIST = {
    **RAW_ALLOWLIST,
    "contracts": frozenset({"contract_name", "currency", "tenant_id"}),
}


def run_raw(
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
    statement: str,
    allowed: Mapping[str, frozenset[str]] = RAW_ALLOWLIST,
    view_access: ViewAccess = NO_VIEWS,
) -> list[dict[str, object]]:
    """Run SQL the query builder would never produce through the real authorizer."""
    monkeypatch.setattr(
        sqlite_module, "build_query", lambda definition, filters, limit: (statement, ())
    )
    connection = open_read_only(path)
    try:
        rows, _ = run_bounded_query(
            connection, invoice_query(), {}, 5, allowed, None, view_access
        )
    finally:
        connection.close()
    return rows


@pytest.fixture
def raw_database(tmp_path: Path) -> Path:
    path = create_database(tmp_path / "raw.sqlite")
    with contextlib.closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            "CREATE TABLE secrets (secret_id INTEGER PRIMARY KEY, api_key TEXT);"
            "INSERT INTO secrets VALUES (1, 'TOP-SECRET-VALUE');"
        )
    return path


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM invoices",
        "UPDATE invoices SET paid = 1",
        "INSERT INTO invoices (invoice_id) VALUES ('x')",
        "REPLACE INTO invoices (invoice_id) VALUES ('x')",
        "DROP TABLE invoices",
        "CREATE TABLE t (a)",
        "CREATE TEMP TABLE t (a)",
        "ALTER TABLE invoices ADD COLUMN x",
        "PRAGMA table_info(invoices)",
        "ATTACH DATABASE ':memory:' AS aux",
        "BEGIN",
        "SAVEPOINT s",
        "ANALYZE",
        'SELECT LENGTH("invoices"."invoice_id") FROM "invoices"',
        'SELECT LOWER("invoices"."invoice_id") FROM "invoices"',
        'SELECT AVG("invoices"."amount_cents") FROM "invoices"',
        "SELECT load_extension('x')",
        "SELECT RANDOM()",
        'SELECT COUNT(*) FROM "secrets"',
        'SELECT 1 FROM "secrets"',
        'SELECT COUNT(*) FROM main."secrets"',
        'SELECT COUNT(*) FROM "INVOICES"',
        "SELECT COUNT(*) FROM sqlite_master",
        "SELECT COUNT(*) FROM sqlite_schema",
        "SELECT COUNT(*) FROM temp.sqlite_temp_master",
        "SELECT COUNT(*) FROM pragma_table_info('invoices')",
        'SELECT (SELECT COUNT(*) FROM "secrets") AS n FROM "invoices"',
        'SELECT COUNT(*) FROM "invoices" WHERE EXISTS (SELECT 1 FROM "secrets")',
    ],
)
def test_authorizer_denies_writes_pragmas_unlisted_functions_and_unlisted_tables(
    monkeypatch: pytest.MonkeyPatch, raw_database: Path, statement: str
) -> None:
    with pytest.raises(UnauthorizedDatabaseAccessError, match="outside the allowlist"):
        run_raw(monkeypatch, raw_database, statement)

    with contextlib.closing(sqlite3.connect(raw_database)) as check:
        assert check.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 6


@pytest.mark.parametrize(
    ("statement", "rows"),
    [
        ('SELECT COUNT(*) AS "n" FROM "invoices"', [{"n": 6}]),
        ('SELECT COUNT(*) AS "n" FROM "invoices" AS "invoices"', [{"n": 6}]),
        ('SELECT COUNT(*) AS "n" FROM main."invoices"', [{"n": 6}]),
        (
            'SELECT COUNT(*) AS "n" FROM "invoices" AS "i"'
            ' JOIN "contracts" AS "c" ON "i"."contract_id" = "c"."contract_id"',
            [{"n": 6}],
        ),
        ('SELECT "contracts"."contract_id" AS "n" FROM "contracts" ORDER BY 1', [{"n": 1}, {"n": 2}, {"n": 3}]),
        ('SELECT 1 AS "n" FROM "contracts" LIMIT 1', [{"n": 1}]),
    ],
)
def test_authorizer_allows_table_reads_that_need_no_column_of_an_allowlisted_table(
    monkeypatch: pytest.MonkeyPatch,
    raw_database: Path,
    statement: str,
    rows: list[dict[str, object]],
) -> None:
    assert run_raw(monkeypatch, raw_database, statement) == rows


@pytest.mark.parametrize(
    "statement",
    [
        'SELECT "contracts"."contract_id" FROM "contracts"',
        'SELECT "contract_id" FROM "contracts"',
        'SELECT rowid FROM "contracts"',
        'SELECT oid FROM "contracts"',
        'SELECT _rowid_ FROM "contracts"',
        'SELECT "contract_name" FROM "contracts" WHERE rowid = 1',
        'SELECT "contract_name" FROM "contracts" ORDER BY "contract_id"',
        'SELECT COUNT(*) FROM "contracts" WHERE rowid = 1',
        'SELECT * FROM "contracts"',
        'SELECT "invoices"."invoice_id" FROM "invoices"'
        ' JOIN "contracts" ON "contracts"."contract_id" = "invoices"."contract_id"',
    ],
)
def test_the_table_level_exemption_never_exposes_an_unlisted_integer_key(
    monkeypatch: pytest.MonkeyPatch, raw_database: Path, statement: str
) -> None:
    # contract_id is the table's rowid alias; only its absence from the allowlist protects it.
    with pytest.raises(DatabaseIndexError):
        run_raw(monkeypatch, raw_database, statement, KEYLESS_ALLOWLIST)

    assert run_raw(
        monkeypatch, raw_database, 'SELECT COUNT(*) AS "n" FROM "contracts"', KEYLESS_ALLOWLIST
    ) == [{"n": 3}]


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT name FROM sqlite_master",
        "SELECT name FROM sqlite_temp_master",
        "SELECT name FROM pragma_table_info('invoices')",
        'SELECT "invoices"."reference" FROM "invoices"',
        'SELECT COUNT(*) FROM "invoices" WHERE "invoices"."reference" = \'NW-2026-01\'',
        'SELECT "secrets"."api_key" FROM "secrets"',
        'SELECT "invoices"."invoice_id" FROM "invoices"'
        ' UNION SELECT "secrets"."api_key" FROM "secrets"',
        'SELECT (SELECT "api_key" FROM "secrets") FROM "invoices"',
        "SELECT 1; SELECT 2",
        "VACUUM",
    ],
)
def test_authorizer_fails_closed_for_reads_outside_the_allowlist(
    monkeypatch: pytest.MonkeyPatch, raw_database: Path, statement: str
) -> None:
    # Denied column reads surface as DatabaseQueryError today; what matters is that they fail.
    with pytest.raises(DatabaseIndexError) as raised:
        run_raw(monkeypatch, raw_database, statement)

    assert "TOP-SECRET-VALUE" not in str(raised.value) + repr(raised.value.__cause__)


def test_authorizer_allows_inside_a_view_only_what_registration_recorded(
    monkeypatch: pytest.MonkeyPatch, raw_database: Path
) -> None:
    with contextlib.closing(sqlite3.connect(raw_database)) as connection:
        connection.executescript(
            "CREATE VIEW open_refs AS SELECT invoice_id, lower(reference) AS ref "
            "FROM invoices WHERE paid = 0;"
            "CREATE VIEW leaky AS SELECT secret_id, api_key FROM secrets;"
        )
    with contextlib.closing(open_read_only(raw_database)) as connection:
        access = inspect_view_access(connection, ["open_refs"])
    allowed = {
        "open_refs": frozenset({"invoice_id", "ref"}),
        "leaky": frozenset({"secret_id", "api_key"}),
    }
    statement = 'SELECT "open_refs"."ref" AS "r" FROM "open_refs" ORDER BY 1 LIMIT 1'

    assert run_raw(monkeypatch, raw_database, statement, allowed, access) == [
        {"r": "ct-2026-01"}
    ]
    for denied, view_access in (
        (statement, NO_VIEWS),
        ('SELECT "leaky"."api_key" FROM "leaky"', access),
    ):
        with pytest.raises(DatabaseIndexError) as raised:
            run_raw(monkeypatch, raw_database, denied, allowed, view_access)
        assert "TOP-SECRET-VALUE" not in str(raised.value) + repr(raised.value.__cause__)


# --- bounds -------------------------------------------------------------------


def test_the_effective_limit_is_the_smallest_of_query_context_and_request(
    extension: SQLiteDatabaseExtension, database: Path
) -> None:
    register(extension, database, [invoice_query("invoices.list", max_rows=4)])
    capped = RetrievalContext(maximum_results=2)

    by_query = value_of(call(extension, "invoices.list"))
    by_context = value_of(call(extension, "invoices.list", ctx=capped))
    by_request = value_of(call(extension, "invoices.list", limit=3))

    assert (by_query["effective_limit"], by_query["row_count"], by_query["truncated"]) == (4, 4, True)
    assert (by_context["effective_limit"], by_context["row_count"], by_context["truncated"]) == (2, 2, True)
    assert (by_request["effective_limit"], by_request["row_count"], by_request["truncated"]) == (3, 3, True)
    with pytest.raises(InvalidDatabaseToolRequestError, match="limit must be between 1 and 2"):
        call(extension, "invoices.list", ctx=capped, limit=3)


@pytest.mark.parametrize("limit", [0, -1, 101, True, "5", 5.5, None])
def test_invalid_limits_are_rejected(
    extension: SQLiteDatabaseExtension, database: Path, limit: object
) -> None:
    register(extension, database, [invoice_query()])

    with pytest.raises(InvalidDatabaseToolRequestError, match="limit must be between 1 and 100"):
        call(extension, "invoices.list", limit=limit)


def test_truncation_is_reported_only_when_more_rows_exist(
    extension: SQLiteDatabaseExtension, database: Path
) -> None:
    register(extension, database, [invoice_query()])

    exact = value_of(call(extension, "invoices.list", limit=6))
    short = value_of(call(extension, "invoices.list", limit=5))

    assert (exact["row_count"], exact["truncated"]) == (6, False)
    assert (short["row_count"], short["truncated"]) == (5, True)


def test_a_query_with_a_hundred_row_cap_stops_at_a_hundred_rows(
    extension: SQLiteDatabaseExtension, tmp_path: Path
) -> None:
    path = create_events_database(tmp_path / "events.sqlite")
    register(extension, path, [page_query()], allowed=EVENT_COLUMNS)

    value = value_of(call(extension, "events.page"))

    assert (value["row_count"], value["truncated"], value["effective_limit"]) == (100, True, 100)
    assert value["rows"][0] == {"events__event_id": 1}
    assert value["rows"][-1] == {"events__event_id": 100}


def test_the_work_budget_stops_full_table_scans_and_leaves_the_source_usable(
    extension: SQLiteDatabaseExtension, tmp_path: Path
) -> None:
    path = create_events_database(tmp_path / "events.sqlite")
    register(extension, path, [scan_query(), lookup_query()], allowed=EVENT_COLUMNS)

    with pytest.raises(DatabaseExecutionBudgetExceededError, match="instruction budget"):
        call(extension, "events.total", {"kind": "no-such-kind"})

    assert value_of(call(extension, "events.by_id", {"event_id": 42}))["rows"] == [
        {"events__event_id": 42}
    ]


def test_a_running_query_stops_at_its_deadline(
    extension: SQLiteDatabaseExtension,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = create_events_database(tmp_path / "events.sqlite")
    register(extension, path, [scan_query()], allowed=EVENT_COLUMNS)
    now = time.monotonic()
    # Only the in-query progress handler sees the later clock, so the cooperative
    # pre-checks pass and the running statement itself must be interrupted.
    monkeypatch.setattr(sqlite_module, "time", SimpleNamespace(monotonic=lambda: now + 7200))

    with pytest.raises(DatabaseDeadlineExceededError, match="SQLite query exceeded its deadline"):
        call(
            extension,
            "events.total",
            {"kind": "no-such-kind"},
            ctx=RetrievalContext(deadline_monotonic=now + 3600),
        )


def test_a_locked_database_fails_after_a_bounded_wait_and_then_recovers(
    extension: SQLiteDatabaseExtension,
    database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register(extension, database, [invoice_query()])
    monkeypatch.setattr(sqlite_module, "SQLITE_BUSY_TIMEOUT_SECONDS", 0.05)
    writer = sqlite3.connect(database, isolation_level=None)
    try:
        writer.execute("BEGIN EXCLUSIVE")
        with pytest.raises(DatabaseQueryError, match="could not open or query"):
            call(extension, "invoices.list")
    finally:
        writer.close()

    assert invoice_ids(call(extension, "invoices.list")) == ALL_INVOICES


def test_a_cell_larger_than_the_value_limit_fails_closed(
    extension: SQLiteDatabaseExtension, tmp_path: Path
) -> None:
    path = tmp_path / "notes.sqlite"
    with contextlib.closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE notes (note_id INTEGER PRIMARY KEY, body TEXT)")
        connection.execute("INSERT INTO notes VALUES (1, ?)", ("x" * 70_000,))
        connection.execute("INSERT INTO notes VALUES (2, 'small')")
        connection.commit()
    note_id, body = ColumnRef("notes", "note_id"), ColumnRef("notes", "body")
    register(
        extension,
        path,
        [
            QueryDefinition(
                "notes.get", "notes", (note_id, body),
                filters=(FilterRule("note_id", note_id),), order_by=(note_id,), max_rows=1,
            )
        ],
        allowed={"notes": ("note_id", "body")},
    )

    with pytest.raises((DatabaseQueryError, DatabaseResultTooLargeError)):
        call(extension, "notes.get", {"note_id": 1})

    assert value_of(call(extension, "notes.get", {"note_id": 2}))["rows"] == [
        {"notes__note_id": 2, "notes__body": "small"}
    ]


@pytest.mark.parametrize(
    ("parameters", "message"),
    [
        pytest.param({"source_id": ""}, "source_id must be a non-empty string", id="empty-source"),
        pytest.param({"source_id": "  "}, "source_id must be a non-empty string", id="blank-source"),
        pytest.param({"source_id": 5}, "source_id must be a non-empty string", id="numeric-source"),
        pytest.param({"source_version": None}, "source_version must be a non-empty string", id="null-version"),
        pytest.param({"query_id": ""}, "query_id must be a non-empty string", id="empty-query"),
        pytest.param({"source_id": "s" * 257}, "exceed their length limits", id="long-source"),
        pytest.param({"query_id": "q" * 65}, "exceed their length limits", id="long-query"),
        pytest.param({"filters": ["a"]}, "filters must be an object", id="filters-list"),
        pytest.param({"filters": {"k" * 65: 1}}, "filters must be an object", id="long-filter-key"),
        pytest.param({"sql": "DROP TABLE invoices"}, "only limit is optional", id="raw-sql"),
    ],
)
def test_malformed_requests_are_rejected_before_any_query_runs(
    extension: SQLiteDatabaseExtension,
    database: Path,
    parameters: dict[str, object],
    message: str,
) -> None:
    register(extension, database, [invoice_query()])
    base = {
        "source_id": "db1",
        "source_version": "v1",
        "query_id": "invoices.list",
        "filters": {},
    }

    with pytest.raises(InvalidDatabaseToolRequestError, match=re.escape(message)):
        extension.run_tool(ToolRequest(QUERY_TOOL_ID, {**base, **parameters}), RetrievalContext())
    with pytest.raises(InvalidDatabaseToolRequestError, match="requires source_id"):
        extension.run_tool(
            ToolRequest(QUERY_TOOL_ID, {"source_id": "db1", "source_version": "v1"}),
            RetrievalContext(),
        )
    with pytest.raises(InvalidDatabaseToolRequestError, match="unknown database tool"):
        extension.run_tool(ToolRequest("databases.raw_sql", base), RetrievalContext())


# --- evidence -----------------------------------------------------------------


def test_evidence_holds_rows_and_provenance_and_the_unit_belongs_to_the_record(
    extension: SQLiteDatabaseExtension, database: Path
) -> None:
    register(
        extension,
        database,
        [
            invoice_query(
                "invoices.list",
                filters=(FilterRule("min_amount", inv("amount_cents"), "gte"),),
                scope=("eq", "tenant-a"),
                max_rows=3,
                unit="USD cents",
            ),
            invoice_query("invoices.plain", max_rows=1),
            count_query("invoices.count", scope=("eq", "tenant-a")),
        ],
    )

    evidence = call(extension, "invoices.list", {"min_amount": 50_000})
    again = call(extension, "invoices.list", {"min_amount": 50_000})
    value = value_of(evidence)

    assert set(value) == {
        "query_id",
        "filters",
        "effective_limit",
        "query_definition_sha256",
        "database_file_state_sha256",
        "read_timestamp_utc",
        "schema_version",
        "row_count",
        "truncated",
        "rows",
    }
    assert value["rows"] == [
        {"invoices__invoice_id": "INV-1001", "invoices__amount_cents": 125000},
        {"invoices__invoice_id": "INV-1003", "invoices__amount_cents": 70000},
        {"invoices__invoice_id": "INV-2001", "invoices__amount_cents": 90000},
    ]
    assert (value["row_count"], value["truncated"], value["effective_limit"]) == (3, True, 3)
    assert value["filters"] == {"min_amount": 50_000}
    assert evidence.unit == "USD cents" and "unit" not in value
    assert evidence.operation == "databases.query"
    assert (evidence.source_id, evidence.source_version) == ("db1", "v1")
    assert evidence.evidence_kind is EvidenceKind.OBSERVED
    assert evidence.node_id is None and evidence.warnings == ()
    assert evidence.locator.scheme == "sqlite-query" and evidence.locator.locator_version == 1
    assert evidence.locator.value == {
        "query_id": "invoices.list",
        "query_definition_sha256": value["query_definition_sha256"],
        "database_file_state_sha256": value["database_file_state_sha256"],
        "read_timestamp_utc": value["read_timestamp_utc"],
        "schema_version": value["schema_version"],
    }
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", value["read_timestamp_utc"])
    assert isinstance(value["schema_version"], int)
    assert uuid.UUID(evidence.execution_id) != uuid.UUID(again.execution_id)
    assert json.loads(json.dumps(evidence.value)) == evidence.value

    plain = call(extension, "invoices.plain")
    counted = call(extension, "invoices.count")
    assert plain.unit is None and plain.evidence_kind is EvidenceKind.OBSERVED
    assert counted.unit is None and counted.evidence_kind is EvidenceKind.CALCULATED
    assert value_of(counted)["rows"] == [{"n": 5}]


def test_unknown_sources_versions_and_queries_are_refused(
    extension: SQLiteDatabaseExtension, database: Path
) -> None:
    register(extension, database, [invoice_query()])

    with pytest.raises(DatabaseNotRegisteredError, match="not registered"):
        call(extension, "invoices.list", ref=source("other"))
    with pytest.raises(DatabaseNotRegisteredError, match="not registered"):
        call(extension, "invoices.list", ref=source(version="v2"))
    with pytest.raises(InvalidDatabaseToolRequestError, match="not approved for this source"):
        call(extension, "invoices.count")


# --- unregister and replacement -----------------------------------------------


def test_unregistering_removes_the_query_tool_for_that_source_only(
    extension: SQLiteDatabaseExtension, database: Path
) -> None:
    queries = [invoice_query()]
    first = register(extension, database, queries, ref=source("db1"))
    second = register(extension, database, queries, ref=source("db2"))
    near = register(extension, database, queries, ref=source("db10"))

    assert extension.unregister_database("db1") == 1

    assert extension.tools_for_source(first) == ()
    with pytest.raises(DatabaseNotRegisteredError, match="not registered"):
        call(extension, "invoices.list", ref=first)
    with pytest.raises(DatabaseNotRegisteredError, match="not registered"):
        tuple(extension.iter_index_records(first, IndexContext()))
    for survivor in (second, near):
        assert len(extension.tools_for_source(survivor)) == 1
        assert invoice_ids(call(extension, "invoices.list", ref=survivor)) == ALL_INVOICES
        assert tuple(extension.iter_index_records(survivor, IndexContext()))
    for other_id in ("db1", "DB2", "db", "db2 ", ""):
        assert extension.unregister_database(other_id) == 0
    assert len(extension.tools_for_source(second)) == 1


def test_unregistering_drops_every_revision_of_a_source_with_its_own_queries(
    extension: SQLiteDatabaseExtension, database: Path, tmp_path: Path
) -> None:
    first = register(extension, database, [invoice_query("invoices.list", max_rows=2)])
    second = register(
        extension,
        create_database(tmp_path / "second.sqlite"),
        [count_query("invoices.count", scope=("eq", "tenant-a"))],
        ref=source(version="v2"),
    )

    assert advertised_query_ids(extension, first) == ["invoices.list"]
    assert advertised_query_ids(extension, second) == ["invoices.count"]
    with pytest.raises(InvalidDatabaseToolRequestError, match="not approved"):
        call(extension, "invoices.list", ref=second)
    assert value_of(call(extension, "invoices.count", ref=second))["rows"] == [{"n": 5}]

    assert extension.unregister_database("db1") == 2

    assert extension.tools_for_source(first) == () and extension.tools_for_source(second) == ()
    assert extension.unregister_database("db1") == 0


def test_replacing_a_sources_queries_requires_unregistering_it_first(
    extension: SQLiteDatabaseExtension, database: Path
) -> None:
    original = [invoice_query("invoices.list", max_rows=2), count_query("invoices.count")]
    replacement = [
        invoice_query("invoices.list", max_rows=5, unit="USD cents"),
        count_query("invoices.size", scope=("eq", "tenant-a")),
    ]
    ref = register(extension, database, original)
    before = value_of(call(extension, "invoices.list"))
    assert (before["effective_limit"], before["row_count"]) == (2, 2)

    with pytest.raises(DatabaseSourceConflictError, match="different database configuration"):
        register(extension, database, replacement)
    assert advertised_query_ids(extension, ref) == ["invoices.count", "invoices.list"]
    assert value_of(call(extension, "invoices.list"))["effective_limit"] == 2

    assert extension.unregister_database("db1") == 1
    register(extension, database, replacement)

    assert advertised_query_ids(extension, ref) == ["invoices.list", "invoices.size"]
    with pytest.raises(InvalidDatabaseToolRequestError, match="not approved for this source"):
        call(extension, "invoices.count")
    after = call(extension, "invoices.list")
    assert (value_of(after)["effective_limit"], value_of(after)["row_count"]) == (5, 5)
    assert after.unit == "USD cents"
    assert value_of(after)["query_definition_sha256"] != before["query_definition_sha256"]


def test_registered_state_does_not_alias_the_callers_configuration(
    extension: SQLiteDatabaseExtension, database: Path
) -> None:
    stored = config([invoice_query("invoices.list", max_rows=2, scope=("eq", "tenant-a"))])
    extension.register_database_config(source(), database, stored)
    before = value_of(call(extension, "invoices.list"))

    stored["queries"][0]["max_rows"] = 100
    stored["queries"][0]["source_filters"][0]["value"] = "tenant-b"
    stored["allowed_columns"]["invoices"].clear()
    stored["queries"].clear()
    after = value_of(call(extension, "invoices.list"))

    assert after["rows"] == before["rows"] and after["effective_limit"] == 2
    assert after["query_definition_sha256"] == before["query_definition_sha256"]


def test_a_persisted_configuration_registers_the_same_source_after_a_restart(
    database: Path,
) -> None:
    first, second = SQLiteDatabaseExtension(), SQLiteDatabaseExtension()
    try:
        version = first.database_source_version(database)
        ref = source(version=version)
        stored = json.dumps(config([rich_query(), invoice_query(scope=("eq", "tenant-a"))]))
        first.register_database_config(ref, database, json.loads(stored))
        second.register_database_config(ref, database, json.loads(stored))
        assert second.database_source_version(database) == version

        filters = {"min_due": "2026-01-01"}
        before = value_of(call(first, "invoices.total_by_contract", filters, ref=ref))
        after = value_of(call(second, "invoices.total_by_contract", filters, ref=ref))
        assert after["rows"] == before["rows"]
        assert after["query_definition_sha256"] == before["query_definition_sha256"]
        assert after["database_file_state_sha256"] == before["database_file_state_sha256"]
        assert second.tools_for_source(ref) == first.tools_for_source(ref)
        assert [n.node_id for n in second.iter_index_records(ref, IndexContext())] == [
            n.node_id for n in first.iter_index_records(ref, IndexContext())
        ]

        change_database(database)
        with pytest.raises(DatabaseSourceChangedError, match="new source version"):
            call(first, "invoices.list", ref=ref)
        new_ref = source(version=first.database_source_version(database))
        assert new_ref.version != version
        first.unregister_database("db1")
        first.register_database_config(new_ref, database, json.loads(stored))
        refreshed = call(first, "invoices.list", ref=new_ref)
        assert value_of(refreshed)["rows"][0] == {
            "invoices__invoice_id": "INV-1001",
            "invoices__amount_cents": 1,
        }
    finally:
        first.close()
        second.close()


def test_a_closed_extension_refuses_every_operation(database: Path) -> None:
    extension = SQLiteDatabaseExtension()
    register(extension, database, [invoice_query()])
    assert extension.health().status is HealthStatus.HEALTHY

    extension.close()
    extension.close()

    assert extension.health().status is HealthStatus.UNAVAILABLE
    operations: list[Callable[[], object]] = [
        lambda: register(extension, database, [invoice_query()]),
        lambda: extension.unregister_database("db1"),
        lambda: extension.database_source_version(database),
        lambda: extension.tools_for_source(source()),
        lambda: call(extension, "invoices.list"),
        lambda: tuple(extension.iter_index_records(source(), IndexContext())),
    ]
    for operation in operations:
        with pytest.raises(ClosedDatabaseIndexError):
            operation()
