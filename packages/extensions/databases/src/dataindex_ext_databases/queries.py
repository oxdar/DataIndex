"""Typed, predeclared query templates; caller values are always bound parameters."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

from dataindex_ext_databases.exceptions import InvalidDatabaseToolRequestError

MAX_QUERY_ROWS = 100
MAX_FILTER_ITEMS = 100
MAX_FILTER_TEXT_LENGTH = 4096
MAX_FILTER_PAYLOAD_BYTES = 16 * 1024
MAX_FILTER_BIND_PARAMETERS = 900
MAX_QUERY_FILTERS = 64
MAX_QUERY_JOINS = 32
MAX_QUERY_COLUMNS = 32

_QUERY_ID = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_FILTER_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ALIAS = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_FILTER_OPERATORS = {
    "eq": "=",
    "ne": "!=",
    "lt": "<",
    "lte": "<=",
    "gt": ">",
    "gte": ">=",
    "in": "IN",
}


@dataclass(frozen=True, slots=True)
class ColumnRef:
    table: str
    column: str

    def __post_init__(self) -> None:
        if not isinstance(self.table, str) or not self.table:
            raise ValueError("column table must be a non-empty string")
        if not isinstance(self.column, str) or not self.column:
            raise ValueError("column name must be a non-empty string")


@dataclass(frozen=True, slots=True)
class FilterRule:
    name: str
    column: ColumnRef
    operator: str = "eq"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _FILTER_NAME.fullmatch(self.name):
            raise ValueError("filter name must be a simple lowercase identifier")
        if not isinstance(self.column, ColumnRef):
            raise ValueError("filter column must be a ColumnRef")
        if not isinstance(self.operator, str) or self.operator not in _FILTER_OPERATORS:
            raise ValueError(f"unsupported filter operator {self.operator!r}")


@dataclass(frozen=True, slots=True)
class FixedFilterRule:
    """A host-set source scope predicate that tool requests cannot override."""

    name: str
    column: ColumnRef
    operator: str
    value: object

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _FILTER_NAME.fullmatch(self.name):
            raise ValueError("fixed filter name must be a simple lowercase identifier")
        if not isinstance(self.column, ColumnRef):
            raise ValueError("fixed filter column must be a ColumnRef")
        if not isinstance(self.operator, str) or self.operator not in _FILTER_OPERATORS:
            raise ValueError(f"unsupported fixed filter operator {self.operator!r}")
        if self.operator == "in":
            if (
                not isinstance(self.value, tuple)
                or not self.value
                or len(self.value) > MAX_FILTER_ITEMS
                or any(item is None or not _is_scalar(item) for item in self.value)
            ):
                raise ValueError("fixed IN filters require a bounded scalar tuple")
        elif not _is_scalar(self.value):
            raise ValueError("fixed filter value must be a bounded scalar")


@dataclass(frozen=True, slots=True)
class JoinRule:
    left: ColumnRef
    right: ColumnRef
    kind: str = "inner"

    def __post_init__(self) -> None:
        if not isinstance(self.left, ColumnRef) or not isinstance(
            self.right, ColumnRef
        ):
            raise ValueError("join endpoints must be ColumnRef values")
        if self.left.table == self.right.table:
            raise ValueError("join endpoints must belong to different tables")
        if not isinstance(self.kind, str) or self.kind not in {"inner", "left"}:
            raise ValueError("join kind must be 'inner' or 'left'")


@dataclass(frozen=True, slots=True)
class AggregateRule:
    function: str
    column: ColumnRef | None
    alias: str

    def __post_init__(self) -> None:
        if not isinstance(self.function, str) or self.function not in {"count", "sum"}:
            raise ValueError("aggregate function must be 'count' or 'sum'")
        if self.function == "sum" and not isinstance(self.column, ColumnRef):
            raise ValueError("sum requires a column")
        if self.column is not None and not isinstance(self.column, ColumnRef):
            raise ValueError("aggregate column must be a ColumnRef or None")
        if not isinstance(self.alias, str) or not _ALIAS.fullmatch(self.alias):
            raise ValueError("aggregate alias must be a simple lowercase identifier")


@dataclass(frozen=True, slots=True)
class QueryDefinition:
    query_id: str
    table: str
    columns: tuple[ColumnRef, ...]
    filters: tuple[FilterRule, ...] = ()
    source_filters: tuple[FixedFilterRule, ...] = ()
    joins: tuple[JoinRule, ...] = ()
    aggregate: AggregateRule | None = None
    group_by: tuple[ColumnRef, ...] = ()
    order_by: tuple[ColumnRef, ...] = ()
    max_rows: int = 100
    unit: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.query_id, str) or not _QUERY_ID.fullmatch(self.query_id):
            raise ValueError("query_id must be a simple lowercase identifier")
        if not isinstance(self.table, str) or not self.table:
            raise ValueError("query table must be a non-empty string")
        for name in (
            "columns",
            "filters",
            "source_filters",
            "joins",
            "group_by",
            "order_by",
        ):
            if not isinstance(getattr(self, name), tuple):
                raise ValueError(f"{name} must be a tuple")
        if len(self.filters) > MAX_QUERY_FILTERS:
            raise ValueError(f"queries support at most {MAX_QUERY_FILTERS} filters")
        if len(self.source_filters) > MAX_QUERY_FILTERS:
            raise ValueError(
                f"queries support at most {MAX_QUERY_FILTERS} fixed source filters"
            )
        if len(self.joins) > MAX_QUERY_JOINS:
            raise ValueError(f"queries support at most {MAX_QUERY_JOINS} joins")
        if len(self.columns) + (self.aggregate is not None) > MAX_QUERY_COLUMNS:
            raise ValueError(
                f"queries support at most {MAX_QUERY_COLUMNS} result columns"
            )
        if any(not isinstance(column, ColumnRef) for column in self.columns):
            raise ValueError("columns must contain only ColumnRef values")
        if any(not isinstance(rule, FilterRule) for rule in self.filters):
            raise ValueError("filters must contain only FilterRule values")
        if any(
            not isinstance(rule, FixedFilterRule) for rule in self.source_filters
        ):
            raise ValueError("source_filters must contain only FixedFilterRule values")
        if any(not isinstance(rule, JoinRule) for rule in self.joins):
            raise ValueError("joins must contain only JoinRule values")
        if any(not isinstance(column, ColumnRef) for column in self.group_by):
            raise ValueError("group_by must contain only ColumnRef values")
        if any(not isinstance(column, ColumnRef) for column in self.order_by):
            raise ValueError("order_by must contain only ColumnRef values")
        if not isinstance(self.max_rows, int) or isinstance(self.max_rows, bool):
            raise ValueError("max_rows must be an integer")
        if not 1 <= self.max_rows <= MAX_QUERY_ROWS:
            raise ValueError(f"max_rows must be between 1 and {MAX_QUERY_ROWS}")
        if self.unit is not None and (
            not isinstance(self.unit, str) or not self.unit.strip()
        ):
            raise ValueError("unit must be a non-empty string or None")
        if len(set(self.filters)) != len(self.filters):
            raise ValueError("filters must not contain duplicate rules")
        filter_names = [rule.name for rule in self.filters]
        if len(set(filter_names)) != len(filter_names):
            raise ValueError("filter names must be unique")
        source_filter_names = [rule.name for rule in self.source_filters]
        if len(set(source_filter_names)) != len(source_filter_names):
            raise ValueError("source filter names must be unique")
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("selected columns must be unique")
        column_aliases = [
            f"{column.table}__{column.column}" for column in self.columns
        ]
        if len(set(column_aliases)) != len(column_aliases):
            raise ValueError("selected column output aliases must be unique")
        if len(set(self.group_by)) != len(self.group_by):
            raise ValueError("group_by columns must be unique")
        if len(set(self.order_by)) != len(self.order_by):
            raise ValueError("order_by columns must be unique")
        if self.aggregate is None:
            if not self.columns or self.group_by:
                raise ValueError("non-aggregate queries need columns and no group_by")
            if not self.order_by:
                raise ValueError("non-aggregate queries require deterministic order_by")
        else:
            if not isinstance(self.aggregate, AggregateRule):
                raise ValueError("aggregate must be an AggregateRule or None")
            output_aliases = {
                f"{column.table}__{column.column}" for column in self.columns
            }
            if self.aggregate.alias in output_aliases:
                raise ValueError("aggregate alias collides with a selected column")
            if self.columns != self.group_by:
                raise ValueError("aggregate query columns must exactly match group_by")
            if not self.columns and self.order_by:
                raise ValueError("ungrouped aggregate queries cannot declare order_by")
            if any(column not in self.group_by for column in self.order_by):
                raise ValueError("aggregate order_by columns must be grouped")

    def to_mapping(self) -> dict[str, object]:
        """Return a JSON-compatible mapping that ``from_mapping`` accepts."""
        return {
            "query_id": self.query_id,
            "table": self.table,
            "columns": [_column_mapping(column) for column in self.columns],
            "filters": [
                {
                    "name": rule.name,
                    "column": _column_mapping(rule.column),
                    "operator": rule.operator,
                }
                for rule in self.filters
            ],
            "source_filters": [
                {
                    "name": rule.name,
                    "column": _column_mapping(rule.column),
                    "operator": rule.operator,
                    "value": (
                        list(rule.value)
                        if isinstance(rule.value, tuple)
                        else rule.value
                    ),
                }
                for rule in self.source_filters
            ],
            "joins": [
                {
                    "left": _column_mapping(rule.left),
                    "right": _column_mapping(rule.right),
                    "kind": rule.kind,
                }
                for rule in self.joins
            ],
            "aggregate": (
                None
                if self.aggregate is None
                else {
                    "function": self.aggregate.function,
                    "column": (
                        None
                        if self.aggregate.column is None
                        else _column_mapping(self.aggregate.column)
                    ),
                    "alias": self.aggregate.alias,
                }
            ),
            "group_by": [_column_mapping(column) for column in self.group_by],
            "order_by": [_column_mapping(column) for column in self.order_by],
            "max_rows": self.max_rows,
            "unit": self.unit,
        }

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> QueryDefinition:
        """Build a validated definition from the ``to_mapping`` form.

        Raises ``ValueError`` for unknown fields, wrong types or invalid rules.
        """
        data = _object(mapping, "query definition", _QUERY_FIELDS)
        aggregate = data.get("aggregate")
        max_rows = data.get("max_rows", 100)
        if not isinstance(max_rows, int) or isinstance(max_rows, bool):
            raise ValueError("max_rows must be an integer")
        return cls(
            query_id=_string(data, "query_id"),
            table=_string(data, "table"),
            columns=tuple(_column(item, "columns") for item in _items(data, "columns")),
            filters=tuple(_filter(item) for item in _items(data, "filters")),
            source_filters=tuple(
                _fixed_filter(item) for item in _items(data, "source_filters")
            ),
            joins=tuple(_join(item) for item in _items(data, "joins")),
            aggregate=None if aggregate is None else _aggregate(aggregate),
            group_by=tuple(_column(item, "group_by") for item in _items(data, "group_by")),
            order_by=tuple(_column(item, "order_by") for item in _items(data, "order_by")),
            max_rows=max_rows,
            unit=_optional_string(data, "unit"),
        )


_QUERY_FIELDS = frozenset(
    {
        "query_id",
        "table",
        "columns",
        "filters",
        "source_filters",
        "joins",
        "aggregate",
        "group_by",
        "order_by",
        "max_rows",
        "unit",
    }
)


def _column_mapping(column: ColumnRef) -> dict[str, object]:
    return {"table": column.table, "column": column.column}


def _object(
    value: object, name: str, fields: frozenset[str]
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object with string keys")
    unknown = sorted(set(value) - fields)
    if unknown:
        raise ValueError(f"{name} has unsupported fields: {unknown}")
    return value


def _items(data: Mapping[str, object], key: str) -> list[object]:
    value = data.get(key, [])
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{key} must be a list")
    return list(value)


def _string(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _optional_string(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{key} must be a string or null")
    return value


def _column(value: object, name: str) -> ColumnRef:
    data = _object(value, name, frozenset({"table", "column"}))
    return ColumnRef(_string(data, "table"), _string(data, "column"))


def _filter(value: object) -> FilterRule:
    data = _object(value, "filter", frozenset({"name", "column", "operator"}))
    return FilterRule(
        _string(data, "name"),
        _column(data.get("column"), "filter column"),
        _string(data, "operator") if "operator" in data else "eq",
    )


def _fixed_filter(value: object) -> FixedFilterRule:
    data = _object(
        value, "source filter", frozenset({"name", "column", "operator", "value"})
    )
    operator = _string(data, "operator")
    filter_value = data.get("value")
    if operator == "in" and isinstance(filter_value, list):
        filter_value = tuple(filter_value)
    return FixedFilterRule(
        _string(data, "name"),
        _column(data.get("column"), "source filter column"),
        operator,
        filter_value,
    )


def _join(value: object) -> JoinRule:
    data = _object(value, "join", frozenset({"left", "right", "kind"}))
    return JoinRule(
        _column(data.get("left"), "join left"),
        _column(data.get("right"), "join right"),
        _string(data, "kind") if "kind" in data else "inner",
    )


def _aggregate(value: object) -> AggregateRule:
    data = _object(value, "aggregate", frozenset({"function", "column", "alias"}))
    column = data.get("column")
    return AggregateRule(
        _string(data, "function"),
        None if column is None else _column(column, "aggregate column"),
        _string(data, "alias"),
    )


def quote_identifier(identifier: str) -> str:
    """Quote one SQLite identifier using the engine's standard escaping rule."""
    return '"' + identifier.replace('"', '""') + '"'


def _column_sql(column: ColumnRef) -> str:
    return f"{quote_identifier(column.table)}.{quote_identifier(column.column)}"


def _filter_value(
    value: object, rule: FilterRule | FixedFilterRule
) -> tuple[str, tuple[object, ...]]:
    if rule.operator == "in":
        if (
            not isinstance(value, (list, tuple))
            or not value
            or len(value) > MAX_FILTER_ITEMS
        ):
            raise InvalidDatabaseToolRequestError(
                f"filter {rule.name!r} must be a non-empty list of at most "
                f"{MAX_FILTER_ITEMS} values"
            )
        values = tuple(value)
        if any(not _is_scalar(item) for item in values):
            raise InvalidDatabaseToolRequestError(
                f"filter {rule.name!r} contains a non-scalar value"
            )
        if any(item is None for item in values):
            raise InvalidDatabaseToolRequestError(
                f"filter {rule.name!r} IN values cannot contain null"
            )
        return (
            "(" + ",".join("?" for _ in values) + ")",
            values,
        )
    if not _is_scalar(value):
        raise InvalidDatabaseToolRequestError(
            f"filter {rule.name!r} must be a scalar value"
        )
    if value is None:
        if rule.operator == "eq":
            return "IS NULL", ()
        if rule.operator == "ne":
            return "IS NOT NULL", ()
        raise InvalidDatabaseToolRequestError(
            f"filter {rule.name!r} cannot compare null with {rule.operator!r}"
        )
    return _FILTER_OPERATORS[rule.operator], (value,)


def _is_scalar(value: object) -> bool:
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, int):
        return -(2**63) <= value <= (2**63 - 1)
    if isinstance(value, str):
        return len(value) <= MAX_FILTER_TEXT_LENGTH
    if isinstance(value, float):
        return math.isfinite(value)
    return False


@dataclass(frozen=True, slots=True)
class BoundFilter:
    """One validated predicate: ``operator`` is a filter operator, ``is_null`` or
    ``is_not_null``; ``values`` holds the bound scalar values."""

    column: ColumnRef
    operator: str
    values: tuple[object, ...]


def bind_filters(
    definition: QueryDefinition,
    filters: Mapping[str, object],
    limit: int,
) -> tuple[BoundFilter, ...]:
    """Validate caller filters and the limit for one approved definition.

    Every backend builds its native query from this result, so the same filter
    names, scalar types and size bounds apply to every database engine.
    """
    expected_filters = {rule.name for rule in definition.filters}
    if set(filters) != expected_filters:
        missing = sorted(expected_filters - set(filters))
        unknown = sorted(set(filters) - expected_filters)
        raise InvalidDatabaseToolRequestError(
            f"filters mismatch: missing={missing} unknown={unknown}"
        )
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= definition.max_rows
    ):
        raise InvalidDatabaseToolRequestError(
            f"limit must be between 1 and {definition.max_rows}"
        )

    bound: list[BoundFilter] = []
    parameters: list[object] = []
    all_filter_rules: tuple[FilterRule | FixedFilterRule, ...] = (
        definition.filters + definition.source_filters
    )
    for rule in all_filter_rules:
        value = rule.value if isinstance(rule, FixedFilterRule) else filters[rule.name]
        operator, values = _filter_value(value, rule)
        if rule.operator == "in":
            bound.append(BoundFilter(rule.column, "in", values))
        elif operator == "IS NULL":
            bound.append(BoundFilter(rule.column, "is_null", ()))
        elif operator == "IS NOT NULL":
            bound.append(BoundFilter(rule.column, "is_not_null", ()))
        else:
            bound.append(BoundFilter(rule.column, rule.operator, values))
        parameters.extend(values)
    if len(parameters) > MAX_FILTER_BIND_PARAMETERS:
        raise InvalidDatabaseToolRequestError(
            f"queries support at most {MAX_FILTER_BIND_PARAMETERS} filter values"
        )
    try:
        filter_size = len(
            json.dumps(
                parameters,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    except (TypeError, ValueError) as error:
        raise InvalidDatabaseToolRequestError(
            "filter values could not be represented safely"
        ) from error
    if filter_size > MAX_FILTER_PAYLOAD_BYTES:
        raise InvalidDatabaseToolRequestError(
            f"filter values exceed the {MAX_FILTER_PAYLOAD_BYTES}-byte limit"
        )
    return tuple(bound)


def build_query(
    definition: QueryDefinition,
    filters: Mapping[str, object],
    limit: int,
) -> tuple[str, tuple[object, ...]]:
    """Return SQLite/DuckDB SQL with ``?`` placeholders and its parameters."""
    bound = bind_filters(definition, filters, limit)
    select_parts = [
        f"{_column_sql(column)} AS "
        f"{quote_identifier(f'{column.table}__{column.column}')}"
        for column in definition.columns
    ]
    if definition.aggregate is not None:
        aggregate = definition.aggregate
        argument = "*" if aggregate.column is None else _column_sql(aggregate.column)
        select_parts.append(
            f"{aggregate.function.upper()}({argument}) AS "
            f"{quote_identifier(aggregate.alias)}"
        )
    sql_parts = [
        "SELECT " + ", ".join(select_parts),
        f"FROM {quote_identifier(definition.table)} "
        f"AS {quote_identifier(definition.table)}",
    ]
    for join in definition.joins:
        sql_parts.append(
            f"{join.kind.upper()} JOIN {quote_identifier(join.right.table)} "
            f"AS {quote_identifier(join.right.table)} ON "
            f"{_column_sql(join.left)} = {_column_sql(join.right)}"
        )

    parameters: list[object] = []
    where_parts: list[str] = []
    for item in bound:
        column_sql = _column_sql(item.column)
        if item.operator == "in":
            placeholders = ",".join("?" for _ in item.values)
            where_parts.append(f"{column_sql} IN ({placeholders})")
        elif item.operator == "is_null":
            where_parts.append(f"{column_sql} IS NULL")
        elif item.operator == "is_not_null":
            where_parts.append(f"{column_sql} IS NOT NULL")
        else:
            where_parts.append(f"{column_sql} {_FILTER_OPERATORS[item.operator]} ?")
        parameters.extend(item.values)
    if where_parts:
        sql_parts.append("WHERE " + " AND ".join(where_parts))
    if definition.aggregate is not None and definition.group_by:
        sql_parts.append(
            "GROUP BY " + ", ".join(_column_sql(column) for column in definition.group_by)
        )
    ordering = result_ordering(definition)
    if ordering:
        sql_parts.append(
            "ORDER BY " + ", ".join(_column_sql(column) for column in ordering)
        )
    sql_parts.append("LIMIT ?")
    parameters.append(limit + 1)
    return " ".join(sql_parts), tuple(parameters)


def result_ordering(definition: QueryDefinition) -> tuple[ColumnRef, ...]:
    """The declared order; grouped aggregates without one order by their groups."""
    if definition.aggregate is not None and not definition.order_by:
        return definition.group_by
    return definition.order_by


def output_keys(definition: QueryDefinition) -> tuple[str, ...]:
    """Result-row keys in select order: ``table__column`` then the aggregate alias."""
    keys = [f"{column.table}__{column.column}" for column in definition.columns]
    if definition.aggregate is not None:
        keys.append(definition.aggregate.alias)
    return tuple(keys)
