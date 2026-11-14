"""Helpers that turn typed values into plain, JSON-serializable structures."""

from __future__ import annotations

import math
from collections.abc import Mapping

from dataindex.models import JsonValue, NativeLocator

MAX_JSON_DEPTH = 16


def plain_json(value: object, depth: int = 0) -> JsonValue:
    """Copy mappings and sequences into plain dicts and lists.

    Rejects non-finite numbers, unsupported types and excessive nesting.
    """
    if depth > MAX_JSON_DEPTH:
        raise ValueError("value is nested too deeply")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("value must be finite")
        return value
    if isinstance(value, Mapping):
        return {str(key): plain_json(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain_json(item, depth + 1) for item in value]
    raise ValueError(f"unsupported value type {type(value).__name__}")


def locator_json(locator: NativeLocator) -> dict[str, JsonValue]:
    return {
        "scheme": locator.scheme,
        "version": locator.locator_version,
        "value": plain_json(locator.value),
    }
