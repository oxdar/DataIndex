"""Turn native driver values into bounded, JSON-safe query evidence rows.

SQLite keeps its own stricter path in ``sqlite.py``. Every other engine returns
richer native types, so they are normalized here in one place:

* ``Decimal`` values stay exact: integral values become ``int`` and the rest
  become their exact decimal string, never a rounded float.
* Dates, times and timestamps become ISO 8601 strings; UUIDs and MongoDB
  ``ObjectId`` values become strings.
* Integers outside the signed 64-bit range become strings, so JSON consumers
  never silently lose precision.
* Binary, nested (lists, documents, structs), interval and non-finite values
  are refused instead of being dumped into evidence.
"""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Iterable, Sequence
from datetime import date, datetime, time
from decimal import Decimal

from dataindex_ext_databases.exceptions import (
    DatabaseResultError,
    DatabaseResultTooLargeError,
)

MAX_RESULT_BYTES = 64 * 1024
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1

JsonScalar = str | int | float | bool | None


def json_scalar(value: object) -> JsonScalar:
    """Return ``value`` as a JSON scalar or raise ``DatabaseResultError``."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value if _INT64_MIN <= value <= _INT64_MAX else str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DatabaseResultError(
                "non-finite values cannot be returned as query evidence"
            )
        return value
    if isinstance(value, Decimal):
        return _decimal(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    value_type = type(value)
    if value_type.__module__.split(".", 1)[0] == "bson":
        if value_type.__name__ == "ObjectId":
            return str(value)
        if value_type.__name__ == "Decimal128":
            return _decimal(value.to_decimal())  # type: ignore[attr-defined]
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise DatabaseResultError(
            "binary values cannot be returned as query evidence"
        )
    raise DatabaseResultError(
        f"a {value_type.__name__} value cannot be returned as query evidence"
    )


def _decimal(value: Decimal) -> JsonScalar:
    if not value.is_finite():
        raise DatabaseResultError(
            "non-finite values cannot be returned as query evidence"
        )
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent >= 0:
        return json_scalar(int(value))
    return format(value, "f")


def collect_rows(
    records: Iterable[Sequence[object]],
    keys: Sequence[str],
    limit: int,
) -> tuple[list[dict[str, JsonScalar]], bool]:
    """Read at most ``limit`` rows; report whether more were available.

    Each row and the whole result must stay within ``MAX_RESULT_BYTES`` of
    compact JSON, the same bound the SQLite path enforces.
    """
    encoded_keys = [_encoded_size(key) for key in keys]
    rows: list[dict[str, JsonScalar]] = []
    result_size = 2
    for index, record in enumerate(records):
        if index == limit:
            return rows, True
        if len(record) != len(keys):
            raise DatabaseResultError("the database returned an unexpected row shape")
        row: dict[str, JsonScalar] = {}
        row_size = 2
        for position, key in enumerate(keys):
            value = json_scalar(record[position])
            row[key] = value
            if position:
                row_size += 1
            row_size += encoded_keys[position] + 1 + _encoded_size(value)
            if row_size > MAX_RESULT_BYTES:
                raise DatabaseResultTooLargeError(
                    f"query row exceeds the {MAX_RESULT_BYTES}-byte limit"
                )
        if rows:
            result_size += 1
        result_size += row_size
        if result_size > MAX_RESULT_BYTES:
            raise DatabaseResultTooLargeError(
                f"query result exceeds the {MAX_RESULT_BYTES}-byte limit"
            )
        rows.append(row)
    return rows, False


def _encoded_size(value: JsonScalar) -> int:
    try:
        return len(
            json.dumps(
                value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        )
    except (TypeError, ValueError) as error:
        raise DatabaseResultError(
            "a query value could not be represented as JSON evidence"
        ) from error
