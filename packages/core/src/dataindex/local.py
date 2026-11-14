"""Defaults and helpers for the PageIndex-style local client.

Local mode serves one user on one machine. It trusts only the installed
first-party free plugins by default, authorizes only the files that user
submitted, and has no entitlement provider, so commercial capabilities stay
denied. Hosted or multi-tenant deployments use ``DataIndexClient.open`` with an
explicit authorizer and trust allowlist instead.

Core never imports plugin packages. Plugins are reached through the registry and
the small duck-typed protocols below.
"""

from __future__ import annotations

import hashlib
import os
import stat
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib import metadata
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from typing import Protocol, runtime_checkable

from packaging.version import InvalidVersion, Version

from dataindex.exceptions import ConfigurationError
from dataindex.models import ExtensionManifest, JsonValue, SourceRef
from dataindex.registry import ENTRY_POINT_GROUP, ExtensionRegistry

FIRST_PARTY_DISTRIBUTIONS = ("dataindex-ext-documents", "dataindex-ext-databases")
DOCUMENTS_PLUGIN_ID = "documents"
DATABASES_PLUGIN_ID = "databases"
LOCAL_DEPLOYMENT = "local"
DEFAULT_STORAGE_PATH = "./.dataindex"
CATALOG_FILE_NAME = "catalog.sqlite3"
MAX_SUBMITTED_FILE_BYTES = 5 * 1024 * 1024
DATABASE_SOURCE_TYPE = "application/vnd.sqlite3"
DOCUMENT_SUFFIXES: Mapping[str, str] = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".text": "text/plain",
    ".html": "text/html",
    ".htm": "text/html",
}
DATABASE_SUFFIXES = frozenset({".sqlite", ".sqlite3", ".db"})
_INSTALL_HINTS = {
    DOCUMENTS_PLUGIN_ID: "pip install dataindex-ext-documents",
    DATABASES_PLUGIN_ID: "pip install dataindex-ext-databases",
}


@runtime_checkable
class DocumentRegistrar(Protocol):
    """What local mode needs from a documents plugin."""

    def register_document(self, source: SourceRef, content: bytes | str) -> None: ...

    def unregister_document(self, source_id: str) -> int: ...


@runtime_checkable
class DatabaseRegistrar(Protocol):
    """What local mode needs from a databases plugin."""

    def database_source_version(self, database_path: str | os.PathLike[str]) -> str: ...

    def register_database_config(
        self,
        source: SourceRef,
        database_path: str | os.PathLike[str],
        config: Mapping[str, JsonValue],
    ) -> None: ...

    def unregister_database(self, source_id: str) -> int: ...


@dataclass(frozen=True, slots=True)
class FileKind:
    kind: str
    source_type: str
    plugin_id: str


def first_party_plugins() -> tuple[tuple[str, str], ...]:
    """Installed first-party free plugin distributions as ``(name, version)``."""
    installed: list[tuple[str, str]] = []
    for name in FIRST_PARTY_DISTRIBUTIONS:
        try:
            installed.append((name, metadata.version(name)))
        except metadata.PackageNotFoundError:
            continue
    return tuple(installed)


def resolve_trusted_plugins(
    trusted_plugins: Iterable[str | tuple[str, str]] | None,
) -> tuple[tuple[str, str], ...]:
    """Turn the ``trusted_plugins`` option into exact ``(name, version)`` pairs.

    ``None`` trusts the installed first-party free plugins. A distribution name
    trusts the version installed right now; a ``(name, version)`` pair trusts that
    exact version. A given list replaces the default, and an empty list trusts
    nothing.
    """
    if trusted_plugins is None:
        return first_party_plugins()
    if isinstance(trusted_plugins, (str, bytes)):
        raise ConfigurationError(
            "trusted_plugins must be a list of distribution names or "
            "(name, version) pairs, not a single string"
        )
    resolved: list[tuple[str, str]] = []
    for item in trusted_plugins:
        if isinstance(item, str) and item.strip():
            try:
                resolved.append((item.strip(), metadata.version(item.strip())))
            except metadata.PackageNotFoundError as error:
                raise ConfigurationError(
                    f"cannot trust {item!r}: that distribution is not installed"
                ) from error
        elif (
            isinstance(item, tuple)
            and len(item) == 2
            and all(isinstance(part, str) and part.strip() for part in item)
        ):
            resolved.append((item[0].strip(), item[1].strip()))
        else:
            raise ConfigurationError(
                "trusted_plugins items must be a distribution name or a "
                "(name, version) pair"
            )
    for name, version in resolved:
        try:
            Version(version)
        except InvalidVersion as error:
            raise ConfigurationError(
                f"{name!r} has an invalid version {version!r}"
            ) from error
    return tuple(dict.fromkeys(resolved))


def load_local_registry(
    trusted: Iterable[tuple[str, str]],
    discovered_entry_points: Iterable[EntryPoint] | None = None,
) -> ExtensionRegistry:
    """Discover entry points once, enable the trusted compatible ones, load them.

    Trusting a plugin in local mode also enables it. A plugin that is missing,
    incompatible, untrusted or unhealthy is reported in the registry statuses and
    never stops the others from loading. ``discovered_entry_points`` exists for
    tests; by default the installed ``dataindex.extensions`` group is used.
    """
    trusted_pairs = tuple(trusted)
    points = tuple(
        entry_points(group=ENTRY_POINT_GROUP)
        if discovered_entry_points is None
        else discovered_entry_points
    )
    probe = ExtensionRegistry(trusted_distributions=trusted_pairs)
    enabled = [
        status.plugin_id
        for status in probe.discover(points)
        if status.trusted and status.compatible
    ]
    registry = ExtensionRegistry(
        trusted_distributions=trusted_pairs, enabled_plugin_ids=enabled
    )
    registry.discover(points)
    registry.load_enabled()
    return registry


def install_hint(plugin_id: str) -> str:
    return _INSTALL_HINTS.get(plugin_id, "install the plugin and trust it")


def free_capabilities(manifest: ExtensionManifest) -> frozenset[str]:
    """Capabilities local mode may authorize: those of a free, unlicensed plugin."""
    if manifest.edition != "free":
        return frozenset()
    return frozenset(
        capability
        for capability in manifest.capabilities
        if capability not in manifest.capability_entitlements
    )


class LocalAuthorizer:
    """Authorize exactly the sources the local user submitted.

    A grant is pinned to one ``SourceRef`` (ID, type and version) and to the
    capabilities of its owning free plugin. Any other source, version, tenant or
    capability is denied. This is source access authorization only; license
    entitlement is checked separately by the dispatcher.
    """

    def __init__(self, tenant_id: str) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")
        self._tenant_id = tenant_id
        self._grants: dict[str, tuple[SourceRef, frozenset[str]]] = {}
        self._lock = threading.Lock()

    def grant(self, source: SourceRef, capabilities: Iterable[str]) -> None:
        with self._lock:
            self._grants[source.source_id] = (source, frozenset(capabilities))

    def revoke(self, source_id: str) -> None:
        with self._lock:
            self._grants.pop(source_id, None)

    def is_authorized(
        self, tenant_id: str, source: SourceRef, capability: str
    ) -> bool:
        if tenant_id != self._tenant_id:
            return False
        with self._lock:
            grant = self._grants.get(source.source_id)
        if grant is None or grant[0] != source:
            return False
        return capability == "source.access" or capability in grant[1]


def classify_file(path: Path) -> FileKind:
    """Map a submitted file to its source type and the plugin that indexes it."""
    suffix = path.suffix.lower()
    if suffix in DOCUMENT_SUFFIXES:
        return FileKind("document", DOCUMENT_SUFFIXES[suffix], DOCUMENTS_PLUGIN_ID)
    if suffix in DATABASE_SUFFIXES:
        return FileKind("database", DATABASE_SOURCE_TYPE, DATABASES_PLUGIN_ID)
    if suffix == ".pdf":
        raise ConfigurationError(
            "PDF files are not supported in DataIndex v0.1.0; convert the PDF to "
            "Markdown or text first"
        )
    raise ConfigurationError(
        f"unsupported file type {suffix or '(no extension)'!r}; DataIndex v0.1.0 "
        f"reads {', '.join(sorted(DOCUMENT_SUFFIXES))} documents and "
        f"{', '.join(sorted(DATABASE_SUFFIXES))} SQLite databases"
    )


def resolve_submitted_path(path: str | os.PathLike[str]) -> Path:
    """Return the absolute path of an existing regular file."""
    if isinstance(path, bytes) or not isinstance(path, (str, os.PathLike)):
        raise ValueError("path must be a str or os.PathLike")
    resolved = Path(path).expanduser().resolve(strict=True)
    if not stat.S_ISREG(resolved.stat().st_mode):
        raise ConfigurationError(f"{resolved.name!r} is not a regular file")
    return resolved


def source_id_for(path: Path, kind: str) -> str:
    """A stable ID from the absolute path, so resubmitting a file is idempotent."""
    digest = hashlib.sha256(
        os.path.normcase(str(path)).encode("utf-8", "surrogatepass")
    ).hexdigest()[:16]
    return f"{'db' if kind == 'database' else 'doc'}-{digest}"


def content_version(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def read_submitted_file(path: Path) -> bytes:
    """Read a submitted file once, refusing anything over the size limit."""
    with path.open("rb") as handle:
        content = handle.read(MAX_SUBMITTED_FILE_BYTES + 1)
    if len(content) > MAX_SUBMITTED_FILE_BYTES:
        raise ConfigurationError(
            f"{path.name!r} is larger than the {MAX_SUBMITTED_FILE_BYTES // (1024 * 1024)} "
            "MiB limit for submitted documents"
        )
    return content


__all__ = [
    "CATALOG_FILE_NAME",
    "DATABASES_PLUGIN_ID",
    "DATABASE_SOURCE_TYPE",
    "DATABASE_SUFFIXES",
    "DEFAULT_STORAGE_PATH",
    "DOCUMENTS_PLUGIN_ID",
    "DOCUMENT_SUFFIXES",
    "FIRST_PARTY_DISTRIBUTIONS",
    "LOCAL_DEPLOYMENT",
    "MAX_SUBMITTED_FILE_BYTES",
    "DatabaseRegistrar",
    "DocumentRegistrar",
    "FileKind",
    "LocalAuthorizer",
    "classify_file",
    "content_version",
    "first_party_plugins",
    "free_capabilities",
    "install_hint",
    "load_local_registry",
    "read_submitted_file",
    "resolve_submitted_path",
    "resolve_trusted_plugins",
    "source_id_for",
]
