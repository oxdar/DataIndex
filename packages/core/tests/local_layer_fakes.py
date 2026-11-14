"""Fake plugin distributions for core-only tests of the local layer.

Core tests must not import ``dataindex_ext_*`` packages, so these stand in for
the first-party documents plugin and for a commercial plugin.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict

from dataindex.models import (
    EvidenceRecord,
    ExtensionManifest,
    HealthResult,
    HealthStatus,
    IndexContext,
    NativeLocator,
    Node,
    ProbeResult,
    RetrievalContext,
    RetrievalToolSpec,
    SourceRef,
    ToolRequest,
)

DOCUMENTS_DISTRIBUTION = "dataindex-ext-documents"
DATABASES_DISTRIBUTION = "dataindex-ext-databases"
PREMIUM_DISTRIBUTION = "dataindex-ext-mock-premium"


def free_manifest(
    plugin_id: str,
    distribution: str,
    *,
    version: str = "0.1.0",
    core_api_range: str = ">=0.1,<0.2",
    capabilities: tuple[str, ...] | None = None,
    source_types: tuple[str, ...] = ("text/plain",),
) -> ExtensionManifest:
    return ExtensionManifest(
        plugin_id=plugin_id,
        distribution=distribution,
        plugin_version=version,
        core_api_range=core_api_range,
        edition="free",
        license_mode="none",
        capabilities=(
            capabilities
            if capabilities is not None
            else (f"{plugin_id}.index", f"{plugin_id}.retrieve")
        ),
        source_types=source_types,
        publisher="DataIndex test fixture",
    )


def documents_manifest(
    *,
    distribution: str = DOCUMENTS_DISTRIBUTION,
    version: str = "0.1.0",
    core_api_range: str = ">=0.1,<0.2",
) -> ExtensionManifest:
    return free_manifest(
        "documents",
        distribution,
        version=version,
        core_api_range=core_api_range,
        capabilities=("document.index", "document.retrieve"),
        source_types=("text/markdown", "text/plain", "text/html"),
    )


def premium_manifest(*, version: str = "0.1.0") -> ExtensionManifest:
    return ExtensionManifest(
        plugin_id="premium-demo",
        distribution=PREMIUM_DISTRIBUTION,
        plugin_version=version,
        core_api_range=">=0.1,<0.2",
        edition="commercial",
        license_mode="entitlement",
        capabilities=("premium.demo.read",),
        capability_entitlements={"premium.demo.read": "premium.demo.read"},
        source_types=("application/vnd.dataindex.mock-premium",),
        publisher="DataIndex test fixture",
    )


class FakeDistribution:
    def __init__(self, manifest: ExtensionManifest, module: str) -> None:
        self.name = manifest.distribution
        self.version = manifest.plugin_version
        self.files = {f"{module}/dataindex-extension.json": json.dumps(asdict(manifest))}

    def read_text(self, path: str) -> str | None:
        return self.files.get(path)


class BasicExtension:
    """A healthy extension without tools; enough for registry loading."""

    def __init__(
        self,
        manifest: ExtensionManifest,
        *,
        health: HealthStatus = HealthStatus.HEALTHY,
    ) -> None:
        self._manifest = manifest
        self._health = health
        self.closed = False

    def manifest(self) -> ExtensionManifest:
        return self._manifest

    def probe(self, source: SourceRef) -> ProbeResult:
        return ProbeResult(supported=source.source_type in self._manifest.source_types)

    def iter_index_records(self, source: SourceRef, ctx: IndexContext) -> Iterator[Node]:
        return iter(())

    def tools(self) -> Sequence[RetrievalToolSpec]:
        return ()

    def run_tool(self, request: ToolRequest, ctx: RetrievalContext) -> EvidenceRecord:
        raise NotImplementedError

    def health(self) -> HealthResult:
        return HealthResult(self._health)

    def close(self) -> None:
        self.closed = True


class FakeDocumentsPlugin(BasicExtension):
    """Implements the document registration protocol used by local mode."""

    def __init__(self, manifest: ExtensionManifest | None = None) -> None:
        super().__init__(manifest if manifest is not None else documents_manifest())
        self.registered: dict[str, bytes] = {}

    def register_document(self, source: SourceRef, content: bytes | str) -> None:
        self.registered[source.source_id] = (
            content.encode() if isinstance(content, str) else bytes(content)
        )

    def unregister_document(self, source_id: str) -> int:
        return 0 if self.registered.pop(source_id, None) is None else 1

    def iter_index_records(self, source: SourceRef, ctx: IndexContext) -> Iterator[Node]:
        yield Node(
            node_id=f"{source.source_id}:root",
            source_id=source.source_id,
            source_version=source.version,
            kind="document",
            title="Root",
            locator=NativeLocator("text-lines", 1, {"line_start": 1, "line_end": 1}),
        )


class FakeEntryPoint:
    def __init__(
        self,
        manifest: ExtensionManifest,
        *,
        extension: BasicExtension | None = None,
        load_error: Exception | None = None,
        module: str | None = None,
    ) -> None:
        self.manifest = manifest
        self.name = manifest.plugin_id
        top_level = module or "dataindex_ext_" + manifest.plugin_id.replace("-", "_")
        self.value = f"{top_level}.plugin:load_plugin"
        self.dist = FakeDistribution(manifest, top_level)
        self.extension = extension
        self.load_error = load_error
        self.load_calls = 0

    def load(self) -> Callable[[], BasicExtension]:
        self.load_calls += 1
        if self.load_error is not None:
            raise self.load_error
        extension = (
            self.extension
            if self.extension is not None
            else BasicExtension(self.manifest)
        )
        return lambda: extension


def documents_entry_point(plugin: FakeDocumentsPlugin | None = None) -> FakeEntryPoint:
    manifest = documents_manifest()
    return FakeEntryPoint(
        manifest,
        extension=plugin if plugin is not None else FakeDocumentsPlugin(manifest),
    )
