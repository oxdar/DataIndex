"""Read-only MongoDB collections through ``pymongo``.

A collection plays the role of a table and each allowlisted dotted field path
(``status``, ``customer.country``) plays the role of a column. ``_id`` is the
primary key. Declared types come from the collection's ``$jsonSchema``
validator when one exists; MongoDB itself does not enforce a schema otherwise.
Read-only views can be allowlisted like collections. The server runs a view's
pipeline with the connecting user's privileges, and ``_id`` is not treated as
a key there because the pipeline may drop or repeat it.

Queries are built here from approved ``QueryDefinition`` values as ``find``
filters or fixed ``$match``/``$group``/``$sort``/``$limit`` pipelines, so no
caller-supplied operator, expression or ``$where`` code can reach the server.
Joins are not supported. Every operation has a server-side ``maxTimeMS`` and
disk use for sorting and grouping is refused.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from dataindex_ext_databases.connections import ConnectionSettings, scrub
from dataindex_ext_databases.exceptions import (
    DatabaseDeadlineExceededError,
    DatabaseIndexError,
    DatabaseQueryError,
    DatabaseResultError,
    InvalidDatabaseConfigurationError,
    InvalidDatabaseToolRequestError,
    UnauthorizedDatabaseAccessError,
)
from dataindex_ext_databases.queries import (
    BoundFilter,
    QueryDefinition,
    bind_filters,
    output_keys,
    result_ordering,
)
from dataindex_ext_databases.results import JsonScalar, collect_rows
from dataindex_ext_databases.schema import (
    MAX_DECLARED_TYPE_CHARS,
    ColumnInfo,
    SchemaInfo,
    TableInfo,
)

DEFAULT_TIMEOUT_SECONDS = 30.0
CONNECT_TIMEOUT_MS = 10_000
PRIMARY_KEY = "_id"
MAX_PATH_SEGMENTS = 16
_UNAUTHORIZED_CODES = frozenset({13, 18})
_COMPARISONS = {
    "eq": "$eq",
    "lt": "$lt",
    "lte": "$lte",
    "gt": "$gt",
    "gte": "$gte",
}


def validate_allowlist(allowed_columns: Mapping[str, frozenset[str]]) -> None:
    """Refuse system collections, operator-like paths and overlapping paths."""
    for collection, paths in allowed_columns.items():
        if (
            collection.startswith("system.")
            or "$" in collection
            or "\x00" in collection
        ):
            raise InvalidDatabaseConfigurationError(
                f"collection {collection!r} cannot be allowlisted"
            )
        for path in paths:
            segments = path.split(".")
            if len(segments) > MAX_PATH_SEGMENTS or any(
                not segment or segment.startswith("$") or "\x00" in segment
                for segment in segments
            ):
                raise InvalidDatabaseConfigurationError(
                    f"field path {path!r} in {collection!r} must be dotted names "
                    "that do not start with '$'"
                )
            if any(other.startswith(path + ".") for other in paths):
                raise InvalidDatabaseConfigurationError(
                    f"field path {path!r} in {collection!r} overlaps a nested path; "
                    "allowlist only leaf fields"
                )


class MongoServer:
    """One registered MongoDB database and its declared field types."""

    family = "mongodb"
    supports_joins = False

    def __init__(self, settings: ConnectionSettings) -> None:
        self.settings = settings
        self._types: dict[tuple[str, str], tuple[str, ...]] = {}

    def inspect(
        self,
        allowed_columns: Mapping[str, frozenset[str]],
        deadline_monotonic: float | None,
    ) -> SchemaInfo:
        validate_allowlist(allowed_columns)
        with self._client("read the collections of", deadline_monotonic) as (
            client,
            _password,
        ):
            infos = _collection_infos(
                client[self.settings.database], sorted(allowed_columns)
            )
        schema, types = describe_collections(infos, allowed_columns)
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
        if query.joins:
            raise InvalidDatabaseToolRequestError("MongoDB queries cannot join")
        bound = bind_filters(query, filters, limit)
        match = build_match(bound, self._types, query.table)
        with self._client("query", deadline_monotonic) as (client, password):
            collection = client[self.settings.database][query.table]
            max_time_ms = max(1, int(_timeout(deadline_monotonic) * 1000))
            read_timestamp = utc_timestamp()
            try:
                if query.aggregate is None:
                    records = _find_rows(collection, query, match, limit, max_time_ms)
                else:
                    records = _aggregate_rows(
                        collection, query, match, limit, max_time_ms
                    )
            except DatabaseIndexError:
                raise
            except Exception as error:
                name = type(error).__name__
                if name in {"ExecutionTimeout", "NetworkTimeout"}:
                    raise DatabaseDeadlineExceededError(
                        "MongoDB query exceeded its deadline"
                    ) from None
                if _is_connection_failure(error):
                    raise self.settings.connection_error(
                        "query", error, password
                    ) from None
                raise DatabaseQueryError(
                    f"approved MongoDB query failed: {scrub(error, password)}"
                ) from None
            rows, truncated = collect_rows(records, output_keys(query), limit)
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise DatabaseDeadlineExceededError(
                "database operation exceeded its cooperative deadline"
            )
        return rows, truncated, read_timestamp

    @contextmanager
    def _client(
        self, action: str, deadline_monotonic: float | None
    ) -> Iterator[tuple[Any, str | None]]:
        self.settings.require_driver()
        timeout = _timeout(deadline_monotonic)
        password = self.settings.password()
        try:
            client = open_client(self.settings, password, timeout)
        except DatabaseIndexError:
            raise
        except Exception as error:
            raise self.settings.connection_error("connect to", error, password) from None
        try:
            yield client, password
        except DatabaseIndexError:
            raise
        except Exception as error:
            raise self.settings.connection_error(action, error, password) from None
        finally:
            client.close()


def open_client(
    settings: ConnectionSettings, password: str | None, timeout_seconds: float
) -> Any:
    """Create an unconnected client; the first operation connects."""
    import pymongo

    options: dict[str, object] = {
        "serverSelectionTimeoutMS": min(
            CONNECT_TIMEOUT_MS, max(1, int(timeout_seconds * 1000))
        ),
        "connectTimeoutMS": CONNECT_TIMEOUT_MS,
        "socketTimeoutMS": int((timeout_seconds + 5) * 1000),
        "tz_aware": True,
        "uuidRepresentation": "standard",
        "retryWrites": False,
    }
    if password is not None:
        options["password"] = password
    return pymongo.MongoClient(settings.url, **options)


def _collection_infos(database: Any, names: Sequence[str]) -> dict[str, dict[str, Any]]:
    """``listCollections`` results for ``names``: type and validator options."""
    infos: dict[str, dict[str, Any]] = {}
    for info in database.list_collections(filter={"name": {"$in": list(names)}}):
        name = info.get("name")
        if name in names:
            infos[name] = {
                "type": info.get("type", "collection"),
                "options": dict(info.get("options") or {}),
            }
        if len(infos) >= len(names):
            break
    return infos


def describe_collections(
    infos: Mapping[str, Mapping[str, Any]],
    allowed_columns: Mapping[str, frozenset[str]],
) -> tuple[SchemaInfo, dict[tuple[str, str], tuple[str, ...]]]:
    missing = sorted(set(allowed_columns) - set(infos))
    if missing:
        raise UnauthorizedDatabaseAccessError(
            f"allowlisted collections or views are absent: {missing}"
        )
    tables: dict[str, TableInfo] = {}
    types: dict[tuple[str, str], tuple[str, ...]] = {}
    digest_source: dict[str, object] = {}
    for name in sorted(allowed_columns):
        info = infos[name]
        kind = info.get("type", "collection")
        if kind not in {"collection", "view"}:
            raise UnauthorizedDatabaseAccessError(
                f"{name!r} is a MongoDB {kind}; only collections and views "
                "can be allowlisted"
            )
        options = info.get("options") or {}
        validator = options.get("validator")
        json_schema = (
            validator.get("$jsonSchema") if isinstance(validator, Mapping) else None
        )
        # A view's pipeline may drop or repeat _id, so it is not a key there.
        is_view = kind == "view"
        paths = sorted(
            allowed_columns[name], key=lambda path: (path != PRIMARY_KEY, path)
        )
        columns: list[ColumnInfo] = []
        for ordinal, path in enumerate(paths):
            bson_types, required = _declared(json_schema, path)
            types[(name, path)] = bson_types
            is_key = path == PRIMARY_KEY and not is_view
            columns.append(
                ColumnInfo(
                    name=path,
                    declared_type="|".join(bson_types)[:MAX_DECLARED_TYPE_CHARS],
                    not_null=is_key
                    or (required and bool(bson_types) and "null" not in bson_types),
                    primary_key_position=1 if is_key else 0,
                    ordinal=ordinal,
                )
            )
        tables[name] = TableInfo(
            name=name,
            columns=tuple(columns),
            foreign_keys=(),
            primary_key_columns=() if is_view else (PRIMARY_KEY,),
            object_kind=kind,
        )
        digest_source[name] = (
            {
                "type": kind,
                "viewOn": options.get("viewOn"),
                "pipeline": options.get("pipeline"),
            }
            if is_view
            else {"type": info.get("type"), "validator": validator}
        )
    digest = hashlib.sha256(
        json.dumps(
            digest_source, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
    ).hexdigest()
    return SchemaInfo(digest, tables), types


def build_match(
    bound: Sequence[BoundFilter],
    types: Mapping[tuple[str, str], tuple[str, ...]],
    collection: str,
) -> dict[str, object]:
    """A ``find`` filter that matches SQL semantics for null and ``ne``."""
    clauses: list[dict[str, object]] = []
    for item in bound:
        path = item.column.column
        declared = types.get((collection, path), ())
        if item.operator == "is_null":
            clauses.append({path: {"$eq": None}})
        elif item.operator == "is_not_null":
            clauses.append({path: {"$ne": None}})
        elif item.operator == "in":
            clauses.append(
                {path: {"$in": [coerce_value(value, declared) for value in item.values]}}
            )
        elif item.operator == "ne":
            # SQL "<>" never matches NULL; MongoDB "$ne" matches missing fields.
            clauses.append(
                {path: {"$nin": [coerce_value(item.values[0], declared), None]}}
            )
        else:
            clauses.append(
                {
                    path: {
                        _COMPARISONS[item.operator]: coerce_value(
                            item.values[0], declared
                        )
                    }
                }
            )
    return {"$and": clauses} if clauses else {}


def coerce_value(value: object, declared: Sequence[str]) -> object:
    """Turn text into the ObjectId, date or decimal a declared field holds."""
    if not isinstance(value, str) or len(declared) != 1:
        return value
    kind = declared[0]
    try:
        if kind == "objectId":
            from bson import ObjectId

            return ObjectId(value)
        if kind == "date":
            moment = datetime.fromisoformat(value)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            return moment
        if kind == "decimal":
            from bson.decimal128 import Decimal128

            number = Decimal(value)
            if not number.is_finite():
                raise ValueError(value)
            return Decimal128(number)
    except (ValueError, TypeError, InvalidOperation, ImportError):
        raise InvalidDatabaseToolRequestError(
            f"filter value {value[:64]!r} does not match its {kind} field"
        ) from None
    except Exception as error:
        if type(error).__name__ == "InvalidId":
            raise InvalidDatabaseToolRequestError(
                f"filter value {value[:64]!r} is not a valid ObjectId"
            ) from None
        raise
    return value


def utc_timestamp() -> str:
    return (
        datetime.now(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _find_rows(
    collection: Any,
    query: QueryDefinition,
    match: Mapping[str, object],
    limit: int,
    max_time_ms: int,
) -> list[tuple[object, ...]]:
    paths = [column.column for column in query.columns]
    projection: dict[str, int] = {path: 1 for path in paths}
    if PRIMARY_KEY not in projection:
        projection[PRIMARY_KEY] = 0
    cursor = collection.find(
        dict(match),
        projection=projection,
        sort=[(column.column, 1) for column in result_ordering(query)],
        limit=limit + 1,
        max_time_ms=max_time_ms,
        allow_disk_use=False,
    )
    try:
        return [
            tuple(_field(document, path) for path in paths) for document in cursor
        ]
    finally:
        cursor.close()


def _aggregate_rows(
    collection: Any,
    query: QueryDefinition,
    match: Mapping[str, object],
    limit: int,
    max_time_ms: int,
) -> list[tuple[object, ...]]:
    aggregate = query.aggregate
    assert aggregate is not None
    groups = [column.column for column in query.group_by]
    group: dict[str, object] = {
        "_id": (
            {
                f"g{index}": {"$ifNull": [f"${path}", None]}
                for index, path in enumerate(groups)
            }
            if groups
            else None
        )
    }
    if aggregate.column is None:
        group["a0"] = {"$sum": 1}
    else:
        present = {"$cond": [{"$gt": [f"${aggregate.column.column}", None]}, 1, 0]}
        if aggregate.function == "count":
            group["a0"] = {"$sum": present}
        else:
            group["a0"] = {"$sum": f"${aggregate.column.column}"}
            group["n0"] = {"$sum": present}
    pipeline: list[dict[str, object]] = []
    if match:
        pipeline.append({"$match": dict(match)})
    pipeline.append({"$group": group})
    ordering = result_ordering(query)
    if ordering:
        pipeline.append(
            {
                "$sort": {
                    f"_id.g{groups.index(column.column)}": 1 for column in ordering
                }
            }
        )
    pipeline.append({"$limit": limit + 1})
    cursor = collection.aggregate(
        pipeline, maxTimeMS=max_time_ms, allowDiskUse=False
    )
    records: list[tuple[object, ...]] = []
    try:
        for document in cursor:
            keys = document.get("_id") or {}
            value = document.get("a0")
            if "n0" in group and not document.get("n0"):
                value = None
            records.append(
                (*(keys.get(f"g{index}") for index in range(len(groups))), value)
            )
    finally:
        cursor.close()
    if not groups and not records:
        # A server returns no group for an empty match; SQL returns one row.
        records.append((0 if aggregate.function == "count" else None,))
    return records


def _field(document: Mapping[str, object], path: str) -> object:
    value: object = document
    for segment in path.split("."):
        if isinstance(value, list):
            raise DatabaseResultError(
                f"field {path!r} is inside an array; arrays cannot be returned "
                "as query evidence"
            )
        if not isinstance(value, Mapping):
            return None
        value = value.get(segment)
    return value


def _declared(json_schema: object, path: str) -> tuple[tuple[str, ...], bool]:
    node = json_schema
    required_chain = True
    for segment in path.split("."):
        if not isinstance(node, Mapping):
            return (), False
        properties = node.get("properties")
        if not isinstance(properties, Mapping) or segment not in properties:
            return (), False
        required = node.get("required")
        required_chain = (
            required_chain
            and isinstance(required, (list, tuple))
            and segment in required
        )
        node = properties[segment]
    if not isinstance(node, Mapping):
        return (), False
    declared = node.get("bsonType", node.get("type"))
    if isinstance(declared, str):
        names: tuple[str, ...] = (declared,)
    elif isinstance(declared, (list, tuple)):
        names = tuple(str(name) for name in declared if isinstance(name, str))
    else:
        names = ()
    return names, required_chain


def _is_connection_failure(error: BaseException) -> bool:
    for kind in type(error).__mro__:
        if kind.__name__ in {"ConnectionFailure", "ConfigurationError"}:
            return True
    return getattr(error, "code", None) in _UNAUTHORIZED_CODES


def _timeout(deadline_monotonic: float | None) -> float:
    if deadline_monotonic is None:
        return DEFAULT_TIMEOUT_SECONDS
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        raise DatabaseDeadlineExceededError(
            "database operation exceeded its cooperative deadline"
        )
    return min(remaining, DEFAULT_TIMEOUT_SECONDS)
