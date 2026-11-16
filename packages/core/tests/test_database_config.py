from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

import pytest

from dataindex.database_config import (
    MAX_ALLOWED_TABLES,
    MAX_COLUMNS_PER_TABLE,
    MAX_SUMMARIZED_QUERIES,
    MAX_SUMMARY_TEXT_CHARS,
    config_digest,
    database_state,
    database_version,
    is_connection_config,
    normalize_database_config,
    query_summaries,
)
from dataindex.workspace import MAX_CONFIG_JSON_BYTES, WorkspaceDocument, WorkspaceRegistry

FIXED_TEXT = "TENANT-SENTINEL-7f3a"
FIXED_NUMBER = 918273645
ALLOWED = {"orders": ["region", "total", "tenant_id", "year"]}


class FakeQuery:
    """Stands in for the databases plugin's QueryDefinition (core must not import it)."""

    def __init__(self, mapping: object) -> None:
        self.mapping = mapping

    def to_mapping(self) -> object:
        return self.mapping


def ref(table: str, column: str) -> dict[str, str]:
    return {"table": table, "column": column}


def tenant_filter(value: object = FIXED_TEXT) -> dict[str, object]:
    return {
        "name": "tenant",
        "column": ref("orders", "tenant_id"),
        "operator": "eq",
        "value": value,
    }


def query(**overrides: object) -> dict[str, object]:
    """A query in the mapping form the databases plugin produces."""
    mapping: dict[str, object] = {
        "query_id": "revenue-by-region",
        "table": "orders",
        "columns": [ref("orders", "region")],
        "filters": [
            {"name": "region", "column": ref("orders", "region"), "operator": "eq"}
        ],
        "source_filters": [
            tenant_filter(),
            {
                "name": "year",
                "column": ref("orders", "year"),
                "operator": "in",
                "value": [FIXED_NUMBER, 2025],
            },
        ],
        "joins": [],
        "aggregate": {
            "function": "sum",
            "column": ref("orders", "total"),
            "alias": "revenue",
        },
        "group_by": [ref("orders", "region")],
        "order_by": [],
        "max_rows": 50,
        "unit": "USD",
    }
    mapping.update(overrides)
    return mapping


def nested(levels: int) -> dict[str, object]:
    value: object = 1
    for _ in range(levels):
        value = {"a": value}
    assert isinstance(value, dict)
    return value


def canonical_size(config: Mapping[str, object]) -> int:
    text = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return len(text.encode("utf-8"))


# ---------------------------------------------------------------------------
# normalize_database_config: canonical form


def test_equal_approvals_normalize_to_the_same_configuration() -> None:
    first = normalize_database_config(
        {"orders": ["total", "region", "total"], "customers": ["name"]},
        [{"query_id": "b"}, {"query_id": "a"}],
    )
    second = normalize_database_config(
        {"customers": ("name",), "orders": {"region", "total"}},
        ({"query_id": "a"}, FakeQuery({"query_id": "b"})),
    )

    assert first == {
        "allowed_columns": {"customers": ["name"], "orders": ["region", "total"]},
        "queries": [{"query_id": "a"}, {"query_id": "b"}],
    }
    assert second == first
    assert list(first["allowed_columns"]) == ["customers", "orders"]  # type: ignore[call-overload]
    assert config_digest(second) == config_digest(first)


def test_any_iterable_of_column_names_is_accepted() -> None:
    columns = (name for name in ("b", "a", "b"))

    config = normalize_database_config({"orders": columns}, [{"query_id": "q"}])

    assert config["allowed_columns"] == {"orders": ["a", "b"]}


def test_query_definitions_are_converted_through_to_mapping() -> None:
    mapping = query(columns=(ref("orders", "region"),), options={1: ("a", "b")})

    config = normalize_database_config(ALLOWED, [FakeQuery(mapping)])

    assert config["queries"] == [
        query(columns=[ref("orders", "region")], options={"1": ["a", "b"]})
    ]
    assert json.loads(json.dumps(config)) == config


def test_the_stored_configuration_keeps_fixed_source_filters() -> None:
    config = normalize_database_config(ALLOWED, [query()])

    assert FIXED_TEXT in json.dumps(config)
    assert str(FIXED_NUMBER) in json.dumps(config)


def test_the_configuration_does_not_alias_the_callers_data() -> None:
    allowed = {"orders": ["region"]}
    source_filters = [tenant_filter()]
    columns = [ref("orders", "region")]
    config = normalize_database_config(
        allowed, [query(source_filters=source_filters, columns=columns)]
    )
    before = json.dumps(config, sort_keys=True)

    allowed["orders"].append("secret")
    allowed["extra"] = ["x"]
    source_filters[0]["value"] = "changed"
    source_filters.append(tenant_filter("added"))
    columns.append(ref("orders", "total"))

    assert json.dumps(config, sort_keys=True) == before


# ---------------------------------------------------------------------------
# normalize_database_config: invalid input


@pytest.mark.parametrize(
    "allowed_columns",
    [None, [], ["orders"], "orders", 5, {}],
    ids=["none", "empty-list", "list", "string", "int", "empty-mapping"],
)
def test_allowed_columns_must_map_at_least_one_table(allowed_columns: object) -> None:
    with pytest.raises(ValueError, match="at least one table"):
        normalize_database_config(allowed_columns, [{"query_id": "q"}])  # type: ignore[arg-type]


@pytest.mark.parametrize("table", ["", "   ", 1, None, ("orders",)])
def test_table_names_must_be_non_empty_strings(table: object) -> None:
    with pytest.raises(ValueError, match="table names must be non-empty strings"):
        normalize_database_config({table: ["id"]}, [{"query_id": "q"}])  # type: ignore[dict-item]


@pytest.mark.parametrize("columns", ["id", b"id", 5, None, 1.5])
def test_columns_must_be_a_list_not_a_single_name(columns: object) -> None:
    with pytest.raises(ValueError, match="'orders' must be a list of column names"):
        normalize_database_config({"orders": columns}, [{"query_id": "q"}])  # type: ignore[dict-item]


@pytest.mark.parametrize(
    "columns",
    [[], (), [""], ["id", "  "], ["id", 3], [None], [b"id"], ["id", ["nested"]]],
)
def test_columns_must_be_non_empty_names(columns: object) -> None:
    with pytest.raises(ValueError, match="'orders' must be a non-empty list of column"):
        normalize_database_config({"orders": columns}, [{"query_id": "q"}])  # type: ignore[dict-item]


def test_the_number_of_tables_is_bounded() -> None:
    at_limit = {f"t{index}": ["c"] for index in range(MAX_ALLOWED_TABLES)}

    config = normalize_database_config(at_limit, [{"query_id": "q"}])

    assert len(config["allowed_columns"]) == MAX_ALLOWED_TABLES  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=f"at most {MAX_ALLOWED_TABLES} tables"):
        normalize_database_config({**at_limit, "one-more": ["c"]}, [{"query_id": "q"}])


def test_the_number_of_columns_per_table_is_bounded() -> None:
    names = [f"c{index:04d}" for index in range(MAX_COLUMNS_PER_TABLE)]

    config = normalize_database_config({"orders": names}, [{"query_id": "q"}])

    assert config["allowed_columns"] == {"orders": names}
    with pytest.raises(ValueError, match=f"more than {MAX_COLUMNS_PER_TABLE} columns"):
        normalize_database_config({"orders": [*names, "one-more"]}, [{"query_id": "q"}])


@pytest.mark.parametrize(
    "queries",
    [None, [], (), "q", b"q", {"query_id": "q"}, 5, iter([{"query_id": "q"}])],
    ids=["none", "empty-list", "empty-tuple", "str", "bytes", "mapping", "int", "iterator"],
)
def test_queries_must_be_a_non_empty_list(queries: object) -> None:
    with pytest.raises(ValueError, match="non-empty list of query definitions"):
        normalize_database_config(ALLOWED, queries)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "item",
    [5, "q", None, ["query_id", "q"], FakeQuery(["query_id", "q"]), FakeQuery(None)],
    ids=["int", "str", "none", "list", "to-mapping-list", "to-mapping-none"],
)
def test_each_query_must_be_a_definition_or_its_mapping(item: object) -> None:
    with pytest.raises(ValueError, match="QueryDefinition or its mapping form"):
        normalize_database_config(ALLOWED, [{"query_id": "ok"}, item])


@pytest.mark.parametrize(
    "item",
    [{}, {"table": "orders"}, {"query_id": 5}, {"query_id": None}, {"query_id": ["q"]}],
)
def test_each_query_needs_a_string_id(item: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="needs a string query_id"):
        normalize_database_config(ALLOWED, [item])


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"x": {1, 2}}, "unsupported value type set"),
        ({"x": b"bytes"}, "unsupported value type bytes"),
        ({"x": object()}, "unsupported value type object"),
        ({"x": float("nan")}, "must be finite"),
        ({"x": float("inf")}, "must be finite"),
        ({"x": nested(40)}, "nested too deeply"),
    ],
    ids=["set", "bytes", "object", "nan", "infinity", "too-deep"],
)
def test_queries_must_be_plain_json(extra: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_database_config(ALLOWED, [{"query_id": "q", **extra}])


def test_a_query_that_refers_to_itself_is_rejected_instead_of_recursing() -> None:
    looping: dict[str, object] = {"query_id": "q"}
    looping["again"] = looping

    with pytest.raises(ValueError, match="nested too deeply"):
        normalize_database_config(ALLOWED, [looping])


# ---------------------------------------------------------------------------
# normalize_database_config: size limit shared with the workspace registry


def test_the_canonical_json_is_limited_to_what_the_workspace_can_store(
    tmp_path: Path,
) -> None:
    def padded(extra: int) -> list[dict[str, object]]:
        return [{"query_id": "q", "note": "x" * extra}]

    allowed = {"t": ["c"]}
    base = canonical_size(normalize_database_config(allowed, padded(0)))

    at_limit = normalize_database_config(allowed, padded(MAX_CONFIG_JSON_BYTES - base))

    assert canonical_size(at_limit) == MAX_CONFIG_JSON_BYTES
    with pytest.raises(ValueError, match=f"exceeds {MAX_CONFIG_JSON_BYTES // 1024} KiB"):
        normalize_database_config(allowed, padded(MAX_CONFIG_JSON_BYTES - base + 1))
    with WorkspaceRegistry(tmp_path / "workspace.sqlite3") as registry:
        registry.upsert(workspace_document(tmp_path, at_limit))
        stored = registry.get("db-1")
    assert stored is not None
    assert stored.config == at_limit


def workspace_document(
    tmp_path: Path, config: Mapping[str, object], version: str = "sqlite:1"
) -> WorkspaceDocument:
    return WorkspaceDocument(
        doc_id="db-1",
        name="sales.db",
        kind="database",
        source_type="application/vnd.sqlite3",
        plugin_id="databases",
        path=str(tmp_path / "sales.db"),
        version=version,
        config=config,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# config_digest and database_version


def test_config_digest_is_a_stable_sha256_of_the_canonical_json() -> None:
    # Digests are stored inside source versions, so the canonical form must not drift.
    assert config_digest({}) == (
        "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    )
    assert config_digest({"b": 1, "a": [1, 2]}) == (
        "94a786c3662bc7beeb598efa7d8cb58d7bea25d6c275ea9785a0230ff1f8c2ba"
    )
    assert config_digest({"k": "\u00e9"}) == (
        "9567d6e8f21bbdcbc2e0140bd733df64007ac8ed4af2329f39121e6349c3c86f"
    )


def test_config_digest_ignores_key_order_but_not_list_order() -> None:
    assert config_digest({"a": 1, "b": {"c": 1, "d": 2}}) == config_digest(
        {"b": {"d": 2, "c": 1}, "a": 1}
    )
    assert config_digest({"a": [1, 2]}) != config_digest({"a": [2, 1]})


@pytest.mark.parametrize(
    "config",
    [{"x": {1, 2}}, {"x": float("nan")}, {"x": b"bytes"}, nested(40)],
    ids=["set", "nan", "bytes", "too-deep"],
)
def test_config_digest_refuses_values_that_are_not_plain_json(
    config: Mapping[str, object],
) -> None:
    with pytest.raises(ValueError):
        config_digest(config)  # type: ignore[arg-type]


def test_version_combines_the_file_state_with_a_short_configuration_digest() -> None:
    config = normalize_database_config(ALLOWED, [query()])

    version = database_version("sha256:abc", config)

    assert re.fullmatch(r"sha256:abc\+q[0-9a-f]{16}", version)
    assert version.endswith(config_digest(config)[:16])
    assert database_version("sha256:abc", config) == version
    assert database_version("sha256:abd", config) != version


def test_changing_what_was_approved_changes_the_source_version() -> None:
    def approved(
        columns: tuple[str, ...] = ("region", "total"), **overrides: object
    ) -> dict[str, object]:
        return dict(normalize_database_config({"orders": list(columns)}, [query(**overrides)]))

    variants = {
        "baseline": approved(),
        "extra column": approved(columns=("region", "total", "year")),
        "max rows": approved(max_rows=51),
        "unit": approved(unit="EUR"),
        "fixed scope": approved(source_filters=[tenant_filter("another-tenant")]),
        "no filters": approved(filters=[]),
        "second query": dict(
            normalize_database_config(
                {"orders": ["region", "total"]}, [query(), {"query_id": "extra"}]
            )
        ),
    }

    versions = {name: database_version("sha256:abc", config) for name, config in variants.items()}

    assert len(set(versions.values())) == len(variants), versions
    assert versions["baseline"] == database_version("sha256:abc", approved())


@pytest.mark.parametrize("state", ["sha256:abc", "postgresql-schema:0123abcd", "a+qb"])
def test_database_state_recovers_the_state_part_of_a_version(state: str) -> None:
    config = normalize_database_config(ALLOWED, [query()])

    assert database_state(database_version(state, config)) == state


# ---------------------------------------------------------------------------
# normalize_database_config: database servers

SERVER_URL = "postgresql+psycopg://reader@db.example.com:5432/sales"


def test_a_server_configuration_keeps_the_connection_without_empty_fields() -> None:
    config = normalize_database_config(
        ALLOWED,
        [query()],
        connection={
            "url": SERVER_URL,
            "engine": "postgresql",
            "schema": None,
            "password_env": "DATAINDEX_SALES_PASSWORD",
        },
    )

    assert config["connection"] == {
        "engine": "postgresql",
        "password_env": "DATAINDEX_SALES_PASSWORD",
        "url": SERVER_URL,
    }
    assert list(config["connection"]) == ["engine", "password_env", "url"]  # type: ignore[arg-type]
    assert is_connection_config(config)
    assert not is_connection_config(normalize_database_config(ALLOWED, [query()]))
    assert not is_connection_config(None)
    assert not is_connection_config({"connection": SERVER_URL})


@pytest.mark.parametrize(
    "connection",
    [
        {},
        {"engine": "postgresql"},
        {"url": ""},
        {"url": 5432},
        {"url": SERVER_URL, 1: "x"},
        {"url": "x" * 4097},
        {"url": SERVER_URL, **{f"field{index}": "x" for index in range(8)}},
        SERVER_URL,
    ],
    ids=["empty", "no-url", "empty-url", "number", "key", "long", "too-many", "string"],
)
def test_a_connection_needs_a_url_and_only_short_string_fields(connection: object) -> None:
    with pytest.raises(ValueError, match="connection"):
        normalize_database_config(ALLOWED, [query()], connection=connection)  # type: ignore[arg-type]


def test_the_connection_is_part_of_the_approved_configuration() -> None:
    def version(**connection: str) -> str:
        config = normalize_database_config(ALLOWED, [query()], connection=connection)
        return database_version("postgresql-schema:1", config)

    assert version(url=SERVER_URL) == version(url=SERVER_URL)
    assert version(url=SERVER_URL) != version(url=SERVER_URL, schema="sales")
    assert version(url=SERVER_URL) != version(url=SERVER_URL.replace("sales", "hr"))
    assert version(url=SERVER_URL) != version(
        url=SERVER_URL, password_env="DATAINDEX_OTHER"
    )


# ---------------------------------------------------------------------------
# query_summaries


def test_a_summary_lists_the_whitelisted_facts_of_each_query() -> None:
    config = normalize_database_config(
        ALLOWED, [query(), {"query_id": "aa-minimal"}]
    )

    assert query_summaries(config) == [
        {"query_id": "aa-minimal", "columns": [], "filters": []},
        {
            "query_id": "revenue-by-region",
            "table": "orders",
            "columns": ["orders.region"],
            "filters": [{"name": "region", "operator": "eq", "column": "orders.region"}],
            "aggregate": {"function": "sum", "alias": "revenue", "column": "orders.total"},
            "unit": "USD",
            "max_rows": 50,
        },
    ]


def test_a_count_aggregate_without_a_column_is_summarized() -> None:
    config = {"queries": [query(aggregate={"function": "count", "column": None, "alias": "n"})]}

    [summary] = query_summaries(config)

    assert summary["aggregate"] == {"function": "count", "alias": "n"}


def test_a_partial_aggregate_keeps_only_its_valid_parts() -> None:
    aggregate = {"function": "count", "alias": 5, "column": {"table": "orders"}}

    [summary] = query_summaries({"queries": [{"query_id": "q", "aggregate": aggregate}]})  # type: ignore[arg-type]

    assert summary["aggregate"] == {"function": "count"}  # type: ignore[index]


def test_fixed_source_filter_values_never_reach_the_summaries() -> None:
    config = normalize_database_config(
        ALLOWED, [query(), query(query_id="other", unit=None, aggregate=None)]
    )

    rendered = json.dumps(query_summaries(config))

    assert FIXED_TEXT not in rendered
    assert str(FIXED_NUMBER) not in rendered
    assert "tenant" not in rendered
    assert "source_filters" not in rendered
    assert "revenue-by-region" in rendered  # the rest of the query is described


def test_unknown_and_tampered_fields_are_not_echoed() -> None:
    leak = "LEAK-7c1e"
    config = {
        "secrets": leak,
        "queries": [
            {
                "query_id": "q1",
                "table": "orders",
                "password": leak,
                "notes": [leak],
                "columns": [{**ref("orders", "id"), "note": leak}],
                "filters": [
                    {
                        "name": "f",
                        "operator": "eq",
                        "column": {**ref("orders", "id"), "note": leak},
                        "value": leak,
                    }
                ],
                "source_filters": [tenant_filter(leak), {"value": leak, "name": leak}],
                "joins": [{"left": ref("a", "b"), "right": ref("c", "d"), "kind": leak}],
                "aggregate": {"function": "count", "alias": "n", "note": leak},
                "group_by": [ref(leak, leak)],
                "order_by": [ref(leak, leak)],
            }
        ],
    }

    summaries = query_summaries(config)  # type: ignore[arg-type]

    assert summaries[0]["query_id"] == "q1"  # type: ignore[index]
    assert leak not in json.dumps(summaries)


@pytest.mark.parametrize(
    "config",
    [
        None,
        [],
        "queries",
        5,
        {},
        {"queries": None},
        {"queries": "q"},
        {"queries": 3},
        {"queries": {"query_id": "q"}},
    ],
)
def test_a_config_without_a_list_of_queries_has_no_summaries(config: object) -> None:
    assert query_summaries(config) == []  # type: ignore[arg-type]


def test_entries_that_are_not_queries_are_skipped() -> None:
    config = {
        "queries": [None, "q", 5, ["query_id"], {}, {"query_id": ""}, {"query_id": 7}, {"query_id": "ok"}]
    }

    assert query_summaries(config) == [{"query_id": "ok", "columns": [], "filters": []}]  # type: ignore[arg-type]


def test_malformed_parts_of_a_query_are_dropped_one_by_one() -> None:
    config = {
        "queries": [
            {
                "query_id": "q",
                "table": 5,
                "columns": [
                    ref("orders", "id"),
                    {"table": "orders"},
                    {"column": "id"},
                    {"table": "", "column": "x"},
                    "orders.id",
                    None,
                ],
                "filters": [
                    {"name": "a", "operator": "eq", "column": ref("orders", "id")},
                    {"name": "b"},
                    {"name": "d", "operator": 5, "column": {"table": "orders"}},
                    {"operator": "eq"},
                    "c",
                ],
                "aggregate": "sum",
                "unit": 5,
                "max_rows": "many",
            }
        ]
    }

    assert query_summaries(config) == [  # type: ignore[arg-type]
        {
            "query_id": "q",
            "columns": ["orders.id"],
            "filters": [
                {"name": "a", "operator": "eq", "column": "orders.id"},
                {"name": "b"},
                {"name": "d"},
            ],
        }
    ]


def test_columns_and_filters_that_are_not_lists_are_empty() -> None:
    config = {"queries": [{"query_id": "q", "columns": ref("orders", "id"), "filters": "x"}]}

    assert query_summaries(config) == [{"query_id": "q", "columns": [], "filters": []}]  # type: ignore[arg-type]


def test_a_null_aggregate_and_unit_are_omitted() -> None:
    [summary] = query_summaries({"queries": [{"query_id": "q", "aggregate": None, "unit": None}]})  # type: ignore[arg-type]

    assert "aggregate" not in summary  # type: ignore[operator]
    assert "unit" not in summary  # type: ignore[operator]


@pytest.mark.parametrize("raw", [True, False, 1.5, "50", None, [50]])
def test_max_rows_is_only_reported_for_real_integers(raw: object) -> None:
    [summary] = query_summaries({"queries": [{"query_id": "q", "max_rows": raw}]})  # type: ignore[arg-type]

    assert "max_rows" not in summary  # type: ignore[operator]


def test_max_rows_is_reported_when_it_is_an_integer() -> None:
    [summary] = query_summaries({"queries": [{"query_id": "q", "max_rows": 50}]})  # type: ignore[arg-type]

    assert summary["max_rows"] == 50  # type: ignore[index]


def test_text_is_clipped_to_a_bounded_length() -> None:
    long = "x" * (MAX_SUMMARY_TEXT_CHARS * 3)
    clipped = "x" * MAX_SUMMARY_TEXT_CHARS
    label = f"{clipped}.{clipped}"
    config = {
        "queries": [
            {
                "query_id": long,
                "table": long,
                "unit": long,
                "columns": [{"table": long, "column": long}],
                "filters": [
                    {"name": long, "operator": long, "column": {"table": long, "column": long}}
                ],
                "aggregate": {
                    "function": long,
                    "alias": long,
                    "column": {"table": long, "column": long},
                },
            }
        ]
    }

    assert query_summaries(config) == [  # type: ignore[arg-type]
        {
            "query_id": clipped,
            "table": clipped,
            "unit": clipped,
            "columns": [label],
            "filters": [{"name": clipped, "operator": clipped, "column": label}],
            "aggregate": {"function": clipped, "alias": clipped, "column": label},
        }
    ]


def test_only_the_first_queries_are_summarized() -> None:
    config = {"queries": [{"query_id": f"q{i}"} for i in range(MAX_SUMMARIZED_QUERIES + 50)]}

    summaries = query_summaries(config)  # type: ignore[arg-type]

    assert len(summaries) == MAX_SUMMARIZED_QUERIES
    assert summaries[0]["query_id"] == "q0"  # type: ignore[index]
    assert summaries[-1]["query_id"] == f"q{MAX_SUMMARIZED_QUERIES - 1}"  # type: ignore[index]


def test_summaries_can_be_built_from_a_configuration_read_back_from_the_workspace(
    tmp_path: Path,
) -> None:
    config = normalize_database_config(ALLOWED, [query()])
    path = tmp_path / "workspace.sqlite3"
    with WorkspaceRegistry(path) as registry:
        registry.upsert(
            workspace_document(tmp_path, config, database_version("sqlite:1", config))
        )
    with WorkspaceRegistry(path) as reopened:
        stored = reopened.get("db-1")

    assert stored is not None
    summaries = query_summaries(stored.config)
    assert [summary["query_id"] for summary in summaries] == ["revenue-by-region"]  # type: ignore[index]
    assert FIXED_TEXT not in json.dumps(summaries)
