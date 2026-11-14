"""Contracts for dereferencing native source locations outside the catalog."""

from __future__ import annotations

from typing import Protocol

from dataindex.models import JsonValue, NativeLocator, SourceRef


class LocatorDereferencer(Protocol):
    """Read one bounded native value from its authoritative source."""

    def dereference(self, source: SourceRef, locator: NativeLocator) -> JsonValue: ...
