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
import json
import os
import re
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
MAX_SUBMITTED_BINARY_FILE_BYTES = 50 * 1024 * 1024
DATABASE_SOURCE_TYPE = "application/vnd.sqlite3"
DUCKDB_SOURCE_TYPE = "application/vnd.duckdb"
# Database servers use this prefix plus the engine name, for example
# ``application/vnd.dataindex.db.postgresql``.
CONNECTION_SOURCE_TYPE_PREFIX = "application/vnd.dataindex.db."
# Mirrors the documents plugin's format table. PDF and the 97-2003 Office formats
# need that plugin's optional extras; it reports how to install them.
DOCUMENT_SUFFIXES: Mapping[str, str] = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".text": "text/plain",
    ".rst": "text/plain",
    ".log": "text/plain",
    ".yaml": "text/plain",
    ".yml": "text/plain",
    ".toml": "text/plain",
    ".ini": "text/plain",
    ".cfg": "text/plain",
    ".html": "text/html",
    ".htm": "text/html",
    ".xhtml": "text/html",
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".rtf": "application/rtf",
    ".epub": "application/epub+zip",
    ".eml": "message/rfc822",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".ndjson": "application/x-ndjson",
    ".xml": "application/xml",
}
BINARY_DOCUMENT_SUFFIXES = frozenset(
    {
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".odt", ".ods", ".odp", ".rtf", ".epub", ".eml",
    }
)
DATABASE_SUFFIXES = frozenset({".sqlite", ".sqlite3", ".db", ".duckdb", ".ddb"})
_DUCKDB_SUFFIXES = frozenset({".duckdb", ".ddb"})
_SQLITE_MAGIC = b"SQLite format 3\x00"
_DUCKDB_MAGIC = b"DUCK"
_URL_AUTHORITY_END = re.compile(r"[/?#]")
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


@runtime_checkable
class ConnectionRegistrar(Protocol):
    """What local mode needs from a databases plugin to use database servers.

    It is separate from ``DatabaseRegistrar`` so a plugin without server support
    still serves database files.
    """

    def describe_connection(
        self, connection: Mapping[str, object]
    ) -> Mapping[str, JsonValue]: ...

    def connection_source_version(self, config: Mapping[str, JsonValue]) -> str: ...

    def register_connection_config(
        self,
        source: SourceRef,
        config: Mapping[str, JsonValue],
        *,
        expected_state: str | None = None,
    ) -> str: ...

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
        return FileKind("database", database_file_type(path), DATABASES_PLUGIN_ID)
    raise ConfigurationError(
        f"unsupported file type {suffix or '(no extension)'!r}; DataIndex reads "
        f"{', '.join(sorted(DOCUMENT_SUFFIXES))} documents and "
        f"{', '.join(sorted(DATABASE_SUFFIXES))} SQLite or DuckDB database files"
    )


def database_file_type(path: Path) -> str:
    """SQLite or DuckDB, from the file header; the suffix decides when it has none."""
    try:
        with path.open("rb") as handle:
            header = handle.read(len(_SQLITE_MAGIC))
    except OSError:
        header = b""
    if header[8:12] == _DUCKDB_MAGIC:
        return DUCKDB_SOURCE_TYPE
    if header.startswith(_SQLITE_MAGIC):
        return DATABASE_SOURCE_TYPE
    if path.suffix.lower() in _DUCKDB_SUFFIXES:
        return DUCKDB_SOURCE_TYPE
    return DATABASE_SOURCE_TYPE


def url_has_password(url: str) -> bool:
    """True when a connection URL carries a password in its user information."""
    _, separator, rest = url.partition("://")
    if not separator:
        return False
    authority = _URL_AUTHORITY_END.split(rest, maxsplit=1)[0]
    user_info, at, _ = authority.rpartition("@")
    return bool(at) and ":" in user_info


def connection_source_id(url: str, schema: str | None) -> str:
    """A stable ID from the password-free URL and schema of a database server."""
    identity = json.dumps(
        {"url": url, "schema": schema}, sort_keys=True, separators=(",", ":")
    )
    return f"db-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"


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


def submitted_file_limit(path: Path) -> int:
    """The size limit for a submitted document; office, PDF and similar files get more."""
    if path.suffix.lower() in BINARY_DOCUMENT_SUFFIXES:
        return MAX_SUBMITTED_BINARY_FILE_BYTES
    return MAX_SUBMITTED_FILE_BYTES


def read_submitted_file(path: Path) -> bytes:
    """Read a submitted file once, refusing anything over its size limit."""
    limit = submitted_file_limit(path)
    with path.open("rb") as handle:
        content = handle.read(limit + 1)
    if len(content) > limit:
        raise ConfigurationError(
            f"{path.name!r} is larger than the {limit // (1024 * 1024)} "
            "MiB limit for submitted documents of this type"
        )
    return content


__all__ = [
    "BINARY_DOCUMENT_SUFFIXES",
    "CATALOG_FILE_NAME",
    "CONNECTION_SOURCE_TYPE_PREFIX",
    "DATABASES_PLUGIN_ID",
    "DATABASE_SOURCE_TYPE",
    "DATABASE_SUFFIXES",
    "DEFAULT_STORAGE_PATH",
    "DOCUMENTS_PLUGIN_ID",
    "DOCUMENT_SUFFIXES",
    "DUCKDB_SOURCE_TYPE",
    "FIRST_PARTY_DISTRIBUTIONS",
    "LOCAL_DEPLOYMENT",
    "MAX_SUBMITTED_BINARY_FILE_BYTES",
    "MAX_SUBMITTED_FILE_BYTES",
    "ConnectionRegistrar",
    "DatabaseRegistrar",
    "DocumentRegistrar",
    "FileKind",
    "LocalAuthorizer",
    "classify_file",
    "connection_source_id",
    "content_version",
    "database_file_type",
    "first_party_plugins",
    "free_capabilities",
    "install_hint",
    "load_local_registry",
    "read_submitted_file",
    "resolve_submitted_path",
    "resolve_trusted_plugins",
    "source_id_for",
    "submitted_file_limit",
    "url_has_password",
]
