"""Canonical form of the typed read-only database configuration.

Local mode persists the approved columns and queries of a submitted database in
its workspace so the source can be registered again after a restart. The form is
plain JSON (``QueryDefinition.to_mapping`` values), so core never imports the
databases plugin. The configuration digest becomes part of the source version:
approving different queries yields a new source revision, and stored evidence
can never be attributed to a configuration that did not produce it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence

from dataindex._json import plain_json
from dataindex.models import JsonValue
from dataindex.workspace import MAX_CONFIG_JSON_BYTES

MAX_ALLOWED_TABLES = 256
MAX_COLUMNS_PER_TABLE = 1_024
MAX_SUMMARIZED_QUERIES = 256
MAX_SUMMARY_TEXT_CHARS = 128
DIGEST_CHARS = 16


def normalize_database_config(
    allowed_columns: Mapping[str, Iterable[str]],
    queries: Sequence[object],
) -> dict[str, JsonValue]:
    """Return the canonical ``{"allowed_columns", "queries"}`` configuration.

    Each query is a ``QueryDefinition`` (anything with ``to_mapping()``) or its
    mapping form. Tables, columns and queries are sorted so the same approval
    always produces the same digest. Detailed validation stays in the databases
    plugin; this only checks shape, JSON compatibility and size.
    """
    if not isinstance(allowed_columns, Mapping) or not allowed_columns:
        raise ValueError("allowed_columns must map at least one table to its columns")
    if len(allowed_columns) > MAX_ALLOWED_TABLES:
        raise ValueError(f"allowed_columns may list at most {MAX_ALLOWED_TABLES} tables")
    normalized_columns: dict[str, JsonValue] = {}
    for table, columns in allowed_columns.items():
        if not isinstance(table, str) or not table.strip():
            raise ValueError("allowed_columns table names must be non-empty strings")
        if isinstance(columns, (str, bytes)) or not isinstance(columns, Iterable):
            raise ValueError(f"the columns of {table!r} must be a list of column names")
        names = list(columns)
        if len(names) > MAX_COLUMNS_PER_TABLE:
            raise ValueError(
                f"{table!r} lists more than {MAX_COLUMNS_PER_TABLE} columns"
            )
        if not names or not all(isinstance(name, str) and name.strip() for name in names):
            raise ValueError(
                f"the columns of {table!r} must be a non-empty list of column names"
            )
        normalized_columns[table] = sorted(set(names))
    if (
        isinstance(queries, (str, bytes, Mapping))
        or not isinstance(queries, Sequence)
        or not queries
    ):
        raise ValueError("queries must be a non-empty list of query definitions")
    mappings: list[JsonValue] = []
    for query in queries:
        to_mapping = getattr(query, "to_mapping", None)
        raw = to_mapping() if callable(to_mapping) else query
        if not isinstance(raw, Mapping):
            raise ValueError(
                "each query must be a QueryDefinition or its mapping form"
            )
        item = plain_json(raw)
        if not isinstance(item, dict) or not isinstance(item.get("query_id"), str):
            raise ValueError("each query needs a string query_id")
        mappings.append(item)
    mappings.sort(key=_query_id)
    config: dict[str, JsonValue] = {
        "allowed_columns": dict(sorted(normalized_columns.items())),
        "queries": mappings,
    }
    if len(_canonical_json(config).encode("utf-8")) > MAX_CONFIG_JSON_BYTES:
        raise ValueError(
            f"the database configuration exceeds {MAX_CONFIG_JSON_BYTES // 1024} KiB"
        )
    return config


def config_digest(config: Mapping[str, JsonValue]) -> str:
    """The hex SHA-256 of the canonical JSON form of a configuration."""
    return hashlib.sha256(_canonical_json(config).encode("utf-8")).hexdigest()


def database_version(file_state_version: str, config: Mapping[str, JsonValue]) -> str:
    """Combine the file-state version with the approved-configuration digest."""
    return f"{file_state_version}+q{config_digest(config)[:DIGEST_CHARS]}"


def query_summaries(config: Mapping[str, JsonValue] | None) -> list[JsonValue]:
    """Describe the approved queries without exposing fixed filter values.

    The workspace file is user-writable, so every field is read defensively and
    only whitelisted, clipped values are returned. Fixed ``source_filters`` are
    never included.
    """
    if not isinstance(config, Mapping):
        return []
    raw = config.get("queries")
    if not isinstance(raw, list):
        return []
    summaries: list[JsonValue] = []
    for item in raw[:MAX_SUMMARIZED_QUERIES]:
        summary = _summarize(item)
        if summary is not None:
            summaries.append(summary)
    return summaries


def _summarize(item: object) -> dict[str, JsonValue] | None:
    if not isinstance(item, Mapping):
        return None
    query_id = _text(item.get("query_id"))
    if query_id is None:
        return None
    summary: dict[str, JsonValue] = {"query_id": query_id}
    table = _text(item.get("table"))
    if table is not None:
        summary["table"] = table
    summary["columns"] = [
        label
        for label in (_label(column) for column in _items(item.get("columns")))
        if label is not None
    ]
    filters: list[JsonValue] = []
    for rule in _items(item.get("filters")):
        if not isinstance(rule, Mapping):
            continue
        name = _text(rule.get("name"))
        if name is None:
            continue
        entry: dict[str, JsonValue] = {"name": name}
        operator = _text(rule.get("operator"))
        if operator is not None:
            entry["operator"] = operator
        column = _label(rule.get("column"))
        if column is not None:
            entry["column"] = column
        filters.append(entry)
    summary["filters"] = filters
    aggregate = item.get("aggregate")
    if isinstance(aggregate, Mapping):
        described: dict[str, JsonValue] = {}
        for key in ("function", "alias"):
            value = _text(aggregate.get(key))
            if value is not None:
                described[key] = value
        column = _label(aggregate.get("column"))
        if column is not None:
            described["column"] = column
        summary["aggregate"] = described
    unit = _text(item.get("unit"))
    if unit is not None:
        summary["unit"] = unit
    max_rows = item.get("max_rows")
    if isinstance(max_rows, int) and not isinstance(max_rows, bool):
        summary["max_rows"] = max_rows
    return summary


def _items(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _label(column: object) -> str | None:
    if not isinstance(column, Mapping):
        return None
    table = _text(column.get("table"))
    name = _text(column.get("column"))
    return None if table is None or name is None else f"{table}.{name}"


def _text(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:MAX_SUMMARY_TEXT_CHARS]


def _query_id(item: JsonValue) -> str:
    assert isinstance(item, dict)
    query_id = item["query_id"]
    assert isinstance(query_id, str)
    return query_id


def _canonical_json(value: object) -> str:
    return json.dumps(
        plain_json(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
