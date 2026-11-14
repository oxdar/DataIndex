from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import MappingProxyType

import pytest

from dataindex.catalog import SCHEMA_VERSION, SQLiteCatalog
from dataindex.exceptions import (
    CatalogError,
    CatalogIntegrityError,
    CrossTenantRelationshipError,
    DuplicateNodeError,
    InvalidEvidenceError,
    NodeNotFoundError,
    SourceNotFoundError,
    StaleSourceVersionError,
    UnsupportedSchemaVersionError,
)
from dataindex.models import (
    EvidenceKind,
    EvidenceRecord,
    JsonValue,
    NativeLocator,
    Node,
    Relationship,
    SourceRef,
)


class MemorySource:
    """Tiny authoritative source fixture keyed by native locator."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str, str], JsonValue] = {}

    def add(self, source: SourceRef, locator: NativeLocator, value: JsonValue) -> None:
        key = json.dumps(locator.value, sort_keys=True)
        self.values[(source.source_id, source.version, key)] = value

    def dereference(self, source: SourceRef, locator: NativeLocator) -> JsonValue:
        key = json.dumps(locator.value, sort_keys=True)
        try:
            return self.values[(source.source_id, source.version, key)]
        except KeyError:
            raise KeyError("locator not found in fixture") from None


def make_source(source_id: str = "manual", version: str = "rev-1") -> SourceRef:
    return SourceRef(source_id, "text/plain", version)


def make_node(
    node_id: str,
    *,
    source_id: str = "manual",
    version: str = "rev-1",
    parent_id: str | None = None,
) -> Node:
    return Node(
        node_id=node_id,
        source_id=source_id,
        source_version=version,
        kind="section",
        title=node_id.title(),
        parent_id=parent_id,
        locator=NativeLocator("memory-key", 1, {"key": node_id}),
        attributes={"ordinal": len(node_id)},
    )


def make_evidence(
    node: Node,
    value: JsonValue,
    *,
    kind: EvidenceKind = EvidenceKind.OBSERVED,
) -> EvidenceRecord:
    return EvidenceRecord(
        source_id=node.source_id,
        source_version=node.source_version,
        node_id=node.node_id,
        locator=node.locator,
        operation="read_fixture_value",
        value=value,
        evidence_kind=kind,
        execution_id=f"run-{node.node_id}",
    )


def seed_source(
    catalog: SQLiteCatalog,
    source: SourceRef | None = None,
    *,
    tenant_id: str = "tenant-a",
) -> SourceRef:
    source = source or make_source()
    catalog.create_source(source, tenant_id=tenant_id)
    return source


def test_persistent_catalog_reopens_hierarchy_graph_and_verified_evidence(
    tmp_path: Path,
) -> None:
    database = tmp_path / "index.sqlite"
    source = make_source()
    root = make_node("root")
    child = make_node("child", parent_id="root")
    native = MemorySource()
    native.add(source, root.locator, "source-backed root")

    catalog = SQLiteCatalog(database)
    assert catalog.schema_version == SCHEMA_VERSION
    catalog.create_source(source)
    catalog.create_node(root)
    catalog.create_node(child)
    evidence_id = catalog.create_evidence(
        make_evidence(root, "source-backed root"), native
    )
    relationship = Relationship(
        relationship_id="root-child",
        subject_node_id=root.node_id,
        predicate="contains",
        object_node_id=child.node_id,
        evidence_ids=(evidence_id,),
        evidence_kind=EvidenceKind.OBSERVED,
        identity_confidence=0.86,
    )
    catalog.create_relationship(relationship)
    catalog.close()

    with SQLiteCatalog(database) as reopened:
        assert reopened.get_source(source.source_id) == source
        assert reopened.get_node("child") == child
        assert reopened.get_node("child").locator == child.locator
        assert [node.node_id for node in reopened.list_children("root")] == ["child"]
        assert reopened.get_evidence(evidence_id) == make_evidence(
            root, "source-backed root"
        )
        assert reopened.get_relationship("root-child") == relationship
        assert reopened.traverse_relationships("root") == (relationship,)


def test_upsert_nodes_rolls_back_the_batch_when_a_parent_is_invalid() -> None:
    catalog = SQLiteCatalog()
    source = seed_source(catalog)
    root = make_node("batch-root")
    invalid_child = Node(
        node_id="batch-child",
        source_id=source.source_id,
        source_version=source.version,
        kind="section",
        title="Child",
        parent_id="missing-parent",
        locator=NativeLocator("memory-key", 1, {"key": "child"}),
    )

    with pytest.raises(NodeNotFoundError):
        catalog.upsert_nodes((root, invalid_child))

    assert catalog.get_node(root.node_id) is None
    assert catalog.get_node(invalid_child.node_id) is None
    catalog.close()


def test_source_revision_update_invalidates_old_snapshot_and_keeps_revision_history() -> None:
    catalog = SQLiteCatalog()
    first_revision = seed_source(catalog)
    root = make_node("old-root")
    child = make_node("old-child", parent_id="old-root")
    catalog.create_node(root)
    catalog.create_node(child)
    native = MemorySource()
    native.add(first_revision, root.locator, "old revision evidence")
    evidence_id = catalog.create_evidence(
        make_evidence(root, "old revision evidence"), native
    )
    relationship = Relationship(
        "old-edge",
        root.node_id,
        "contains",
        child.node_id,
        (evidence_id,),
        EvidenceKind.OBSERVED,
    )
    catalog.create_relationship(relationship)

    second_revision = make_source(version="rev-2")
    catalog.update_source(second_revision)

    assert catalog.get_node(root.node_id) is None
    assert catalog.get_node(child.node_id) is None
    assert catalog.get_evidence(evidence_id) is None
    assert catalog.get_relationship(relationship.relationship_id) is None
    assert catalog.get_source(first_revision.source_id) == second_revision
    assert set(catalog.list_source_revisions(first_revision.source_id)) == {
        "rev-1",
        "rev-2",
    }
    with pytest.raises(StaleSourceVersionError):
        catalog.create_node(root)
    catalog.close()


def test_source_and_node_mutations_reject_duplicates_missing_parents_and_cycles() -> None:
    catalog = SQLiteCatalog()
    seed_source(catalog)
    root = make_node("root")
    child = make_node("child", parent_id="root")
    catalog.create_node(root)
    catalog.create_node(child)

    with pytest.raises(DuplicateNodeError):
        catalog.create_node(root)
    with pytest.raises(NodeNotFoundError):
        catalog.create_node(make_node("orphan", parent_id="missing"))
    with pytest.raises(CatalogIntegrityError, match="children"):
        catalog.remove_node("root")
    with pytest.raises(CatalogIntegrityError, match="cycle"):
        catalog.update_node(make_node("root", parent_id="child"))

    other_source = make_source("other")
    catalog.create_source(other_source)
    catalog.create_node(make_node("other-root", source_id="other"))
    with pytest.raises(CatalogIntegrityError, match="same source revision"):
        catalog.create_node(make_node("cross-parent", parent_id="other-root"))
    with pytest.raises(SourceNotFoundError):
        catalog.update_source(SourceRef("missing", "text/plain", "rev-1"))
    catalog.close()


def test_catalog_persists_general_json_mapping_values() -> None:
    catalog = SQLiteCatalog()
    seed_source(catalog)
    node = Node(
        node_id="mapping-node",
        source_id="manual",
        source_version="rev-1",
        kind="section",
        title="Mapping",
        locator=NativeLocator(
            "memory-key", 1, MappingProxyType({"key": "mapping-node"})
        ),
        attributes=MappingProxyType(
            {"nested": MappingProxyType({"rank": 1})}
        ),
    )

    catalog.create_node(node)

    assert catalog.get_node(node.node_id) == node
    catalog.close()


def test_evidence_verifier_rejects_missing_sources_stale_revisions_and_fabrication() -> None:
    catalog = SQLiteCatalog()
    source = seed_source(catalog)
    node = make_node("terms")
    catalog.create_node(node)
    native = MemorySource()
    native.add(source, node.locator, "Net 30")

    with pytest.raises(SourceNotFoundError):
        catalog.verify_evidence(
            EvidenceRecord(
                source_id="unknown",
                source_version="rev-1",
                locator=node.locator,
                operation="read",
                value="Net 30",
                evidence_kind=EvidenceKind.OBSERVED,
                execution_id="run-missing",
            ),
            native,
        )
    with pytest.raises(InvalidEvidenceError, match="does not match"):
        catalog.verify_evidence(make_evidence(node, "Net 60"), native)
    fabricated_locator = NativeLocator("memory-key", 1, {"key": "fabricated"})
    fabricated = EvidenceRecord(
        source_id=source.source_id,
        source_version=source.version,
        node_id=node.node_id,
        locator=fabricated_locator,
        operation="read",
        value="Net 30",
        evidence_kind=EvidenceKind.OBSERVED,
        execution_id="run-fabricated",
    )
    with pytest.raises(InvalidEvidenceError, match="locator"):
        catalog.verify_evidence(fabricated, native)

    catalog.update_source(make_source(version="rev-2"))
    with pytest.raises(StaleSourceVersionError):
        catalog.verify_evidence(make_evidence(node, "Net 30"), native)
    catalog.close()


def test_unbound_or_oversized_evidence_and_unproven_relationships_are_rejected() -> None:
    catalog = SQLiteCatalog()
    source = seed_source(catalog)
    root = make_node("root")
    child = make_node("child")
    catalog.create_node(root)
    catalog.create_node(child)
    native = MemorySource()
    native.add(source, root.locator, "root value")

    with pytest.raises(InvalidEvidenceError, match="resolve"):
        catalog.create_evidence(make_evidence(child, "child value"), native)
    oversized = "x" * (64 * 1024 + 1)
    with pytest.raises(InvalidEvidenceError, match="byte limit"):
        catalog.create_evidence(make_evidence(root, oversized), native)
    unproven = Relationship(
        "unproven",
        "root",
        "references",
        "child",
        ("made-up-evidence-id",),
        EvidenceKind.OBSERVED,
    )
    with pytest.raises(InvalidEvidenceError, match="missing or stale"):
        catalog.create_relationship(unproven)
    catalog.close()


def test_cross_tenant_links_and_evidence_kind_upgrades_are_rejected() -> None:
    catalog = SQLiteCatalog()
    source_a = seed_source(catalog, make_source("a"), tenant_id="tenant-a")
    seed_source(catalog, make_source("b"), tenant_id="tenant-b")
    node_a = make_node("node-a", source_id="a")
    node_b = make_node("node-b", source_id="b")
    node_c = make_node("node-c", source_id="a")
    catalog.create_node(node_a)
    catalog.create_node(node_b)
    catalog.create_node(node_c)
    native = MemorySource()
    native.add(source_a, node_a.locator, "value-a")
    evidence_id = catalog.create_evidence(
        make_evidence(node_a, "value-a"), native
    )

    cross_tenant = Relationship(
        "cross-tenant",
        node_a.node_id,
        "references",
        node_b.node_id,
        (evidence_id,),
        EvidenceKind.OBSERVED,
    )
    with pytest.raises(CrossTenantRelationshipError):
        catalog.create_relationship(cross_tenant)

    inferred = EvidenceRecord(
        source_id=source_a.source_id,
        source_version=source_a.version,
        node_id=node_a.node_id,
        locator=node_a.locator,
        operation="infer_relation",
        value={"related": True},
        evidence_kind=EvidenceKind.MODEL_INFERRED,
        execution_id="run-inferred",
    )
    inferred_id = catalog.create_evidence(inferred, native)
    mismatched_kind = Relationship(
        "mismatched-kind",
        node_a.node_id,
        "references",
        node_c.node_id,
        (inferred_id,),
        EvidenceKind.OBSERVED,
    )
    with pytest.raises(InvalidEvidenceError, match="kind"):
        catalog.create_relationship(mismatched_kind)
    inferred_relationship = Relationship(
        "inferred",
        node_a.node_id,
        "possibly-references",
        node_c.node_id,
        (inferred_id,),
        EvidenceKind.MODEL_INFERRED,
        identity_confidence=0.42,
    )
    catalog.create_relationship(inferred_relationship)
    persisted = catalog.get_relationship("inferred")
    assert persisted.evidence_kind is EvidenceKind.MODEL_INFERRED
    assert persisted.identity_confidence == 0.42
    catalog.close()


def test_node_and_source_removal_clean_up_their_catalog_records() -> None:
    catalog = SQLiteCatalog()
    seed_source(catalog)
    root = make_node("root")
    child = make_node("child", parent_id="root")
    catalog.create_node(root)
    catalog.create_node(child)

    catalog.remove_node(child.node_id)
    assert catalog.get_node(child.node_id) is None
    catalog.remove_node(root.node_id)
    catalog.remove_source("manual")
    assert catalog.get_source("manual") is None
    catalog.close()


def test_deterministic_search_and_graph_traversal_are_bounded() -> None:
    catalog = SQLiteCatalog()
    source = seed_source(catalog)
    nodes = {
        key: make_node(key)
        for key in ("alpha", "beta", "gamma", "delta")
    }
    for node in nodes.values():
        catalog.create_node(node)
    native = MemorySource()
    evidence_ids: dict[str, str] = {}
    for key, node in nodes.items():
        value = f"value-{key}"
        native.add(source, node.locator, value)
        evidence_ids[key] = catalog.create_evidence(
            make_evidence(node, value), native
        )
    for index, (subject, object_) in enumerate(
        (("alpha", "beta"), ("beta", "gamma"), ("gamma", "delta"))
    ):
        catalog.create_relationship(
            Relationship(
                f"edge-{index}",
                subject,
                "next",
                object_,
                (evidence_ids[subject],),
                EvidenceKind.OBSERVED,
            )
        )

    assert [node.node_id for node in catalog.search_nodes("ALP")] == ["alpha"]
    assert [node.node_id for node in catalog.list_children()] == [
        "alpha",
        "beta",
        "delta",
        "gamma",
    ]
    assert [edge.relationship_id for edge in catalog.traverse_relationships(
        "alpha", max_depth=1
    )] == ["edge-0"]
    assert [edge.relationship_id for edge in catalog.traverse_relationships(
        "alpha", max_depth=3, limit=2
    )] == ["edge-0", "edge-1"]
    with pytest.raises(ValueError, match="limit"):
        catalog.search_nodes("alpha", limit=1001)
    with pytest.raises(ValueError, match="max_depth"):
        catalog.traverse_relationships("alpha", max_depth=17)
    catalog.close()


def test_unknown_schema_version_is_not_silently_opened(tmp_path: Path) -> None:
    database = tmp_path / "future.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version = 99")
    connection.close()

    with pytest.raises(UnsupportedSchemaVersionError):
        SQLiteCatalog(database)


def test_a_file_that_is_not_a_catalog_raises_a_typed_error_and_is_released(
    tmp_path: Path,
) -> None:
    database = tmp_path / "damaged.sqlite"
    damaged = b"this is not a database" * 50
    database.write_bytes(damaged)

    with pytest.raises(CatalogError, match="could not read the catalog") as error:
        SQLiteCatalog(database)

    assert isinstance(error.value.__cause__, sqlite3.DatabaseError)
    assert database.read_bytes() == damaged
    database.unlink()  # a handle left open would block this on Windows


def test_an_unopenable_catalog_path_raises_a_typed_error(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="could not read the catalog"):
        SQLiteCatalog(tmp_path / "missing-directory" / "catalog.sqlite")
