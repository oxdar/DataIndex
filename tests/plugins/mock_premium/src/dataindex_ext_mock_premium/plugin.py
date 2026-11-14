"""Synthetic commercial extension for DataIndex contract tests only."""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence

from dataindex.models import (
    DataIndexExtension,
    EvidenceKind,
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

PLUGIN_ID = "premium-demo"
DISTRIBUTION = "dataindex-ext-mock-premium"
PLUGIN_VERSION = "0.1.0"
SOURCE_TYPE = "application/vnd.dataindex.mock-premium"
TOOL_ID = "premium.demo_lookup"
ENTITLEMENT_ID = "premium.demo.read"

_MANIFEST = ExtensionManifest(
    plugin_id=PLUGIN_ID,
    distribution=DISTRIBUTION,
    plugin_version=PLUGIN_VERSION,
    core_api_range=">=0.1,<0.2",
    edition="commercial",
    license_mode="entitlement",
    capabilities=(ENTITLEMENT_ID,),
    source_types=(SOURCE_TYPE,),
    publisher="DataIndex synthetic test fixture",
    capability_entitlements={ENTITLEMENT_ID: ENTITLEMENT_ID},
)

_TOOL = RetrievalToolSpec(
    tool_id=TOOL_ID,
    input_schema={
        "type": "object",
        "properties": {
            "source_id": {"type": "string", "minLength": 1},
            "source_version": {"type": "string", "minLength": 1},
            "query": {"type": "string", "enum": ["summary"]},
        },
        "required": ["source_id", "source_version", "query"],
        "additionalProperties": False,
    },
    capability=ENTITLEMENT_ID,
    maximum_results=1,
)


class MockPremiumExtension(DataIndexExtension):
    """Return a fixed synthetic value without reading source data."""

    def __init__(self) -> None:
        self.run_calls = 0
        self._closed = False

    def manifest(self) -> ExtensionManifest:
        return _MANIFEST

    def probe(self, source: SourceRef) -> ProbeResult:
        self._ensure_open()
        return ProbeResult(source.source_type == SOURCE_TYPE)

    def iter_index_records(
        self, _source: SourceRef, _ctx: IndexContext
    ) -> Iterator[Node]:
        self._ensure_open()
        return iter(())

    def tools(self) -> Sequence[RetrievalToolSpec]:
        self._ensure_open()
        return (_TOOL,)

    def run_tool(
        self, request: ToolRequest, _ctx: RetrievalContext
    ) -> EvidenceRecord:
        self._ensure_open()
        if request.tool_id != TOOL_ID:
            raise ValueError(f"unsupported mock premium tool {request.tool_id!r}")
        self.run_calls += 1
        source_id = request.parameters["source_id"]
        source_version = request.parameters["source_version"]
        return EvidenceRecord(
            source_id=source_id,
            source_version=source_version,
            locator=NativeLocator(
                "mock-premium",
                1,
                {"fixture": "premium-demo", "query": "summary"},
            ),
            operation=TOOL_ID,
            value={"result": "Synthetic premium demo result."},
            evidence_kind=EvidenceKind.OBSERVED,
            execution_id=str(uuid.uuid4()),
        )

    def health(self) -> HealthResult:
        if self._closed:
            return HealthResult(HealthStatus.UNAVAILABLE, "extension is closed")
        return HealthResult(HealthStatus.HEALTHY)

    def close(self) -> None:
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("mock premium extension is closed")


def load_plugin() -> MockPremiumExtension:
    return MockPremiumExtension()
