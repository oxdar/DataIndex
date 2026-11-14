from __future__ import annotations

import pytest

from dataindex.models import (
    EvidenceKind,
    EvidenceRecord,
    ExtensionManifest,
    NativeLocator,
    Node,
    Relationship,
    SourceRef,
)


def test_source_and_evidence_models_preserve_native_provenance() -> None:
    source = SourceRef("contract", "text/markdown", "sha256:abc")
    locator = NativeLocator("markdown-heading", 1, {"anchor": "payment-terms"})
    evidence = EvidenceRecord(
        source_id=source.source_id,
        source_version=source.version,
        locator=locator,
        operation="read_section",
        value="Payment is due in 30 days.",
        evidence_kind=EvidenceKind.OBSERVED,
        execution_id="run-1",
        node_id="terms",
    )

    assert evidence.source_id == source.source_id
    assert evidence.source_version == "sha256:abc"
    assert evidence.locator.value["anchor"] == "payment-terms"


def test_models_reject_invalid_native_locator_values() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        NativeLocator("example", 1, {"coordinate": float("nan")})


def test_manifest_checks_core_api_range_and_rejects_unknown_fields() -> None:
    manifest = ExtensionManifest(
        plugin_id="documents",
        distribution="dataindex-ext-documents",
        plugin_version="0.1.0",
        core_api_range=">=0.1,<0.2",
        edition="free",
        license_mode="none",
        capabilities=("index", "retrieve"),
        source_types=("text/markdown",),
        publisher="example",
    )

    assert manifest.supports_core_api("0.1")
    assert not manifest.supports_core_api("0.2")
    with pytest.raises(ValueError, match="unknown"):
        ExtensionManifest.from_mapping(
            {
                "plugin_id": "documents",
                "distribution": "dataindex-ext-documents",
                "plugin_version": "0.1.0",
                "core_api_range": ">=0.1,<0.2",
                "edition": "free",
                "license_mode": "none",
                "capabilities": ["index"],
                "source_types": ["text/markdown"],
                "publisher": "example",
                "typo": True,
            }
        )


def test_commercial_manifest_requires_entitlement_per_capability() -> None:
    with pytest.raises(ValueError, match="each declare an entitlement"):
        ExtensionManifest(
            plugin_id="premium",
            distribution="dataindex-ext-premium",
            plugin_version="0.1.0",
            core_api_range=">=0.1,<0.2",
            edition="commercial",
            license_mode="entitlement",
            capabilities=("retrieve",),
            source_types=("application/example",),
            publisher="example",
        )


def test_relationship_requires_evidence() -> None:
    with pytest.raises(ValueError, match="evidence IDs"):
        Relationship(
            "rel-1",
            "node-a",
            "references",
            "node-b",
            (),
            EvidenceKind.OBSERVED,
        )


def test_relationship_identity_confidence_is_bounded() -> None:
    with pytest.raises(ValueError, match="identity_confidence"):
        Relationship(
            "rel-1",
            "node-a",
            "references",
            "node-b",
            ("evidence-1",),
            EvidenceKind.OBSERVED,
            identity_confidence=1.1,
        )


def test_node_requires_a_native_locator() -> None:
    node = Node(
        node_id="section-1",
        source_id="guide",
        source_version="revision-1",
        kind="section",
        title="Overview",
        locator=NativeLocator("text-range", 1, {"start": 0, "end": 42}),
    )

    assert node.parent_id is None
