from __future__ import annotations

import pytest

from dataindex.hierarchy import build_hierarchy
from dataindex.models import NativeLocator, Node


def make_node(node_id: str) -> Node:
    return Node(
        node_id=node_id,
        source_id="manual",
        source_version="rev-1",
        kind="section",
        title=node_id,
        locator=NativeLocator("section", 1, {"ordinal": len(node_id)}),
    )


def test_build_hierarchy_attaches_to_nearest_preceding_shallower_level() -> None:
    nodes = tuple(make_node(node_id) for node_id in ("a", "b", "c", "d", "e"))

    result = build_hierarchy(nodes, (1, 2, 2, 1, 3))

    assert [node.parent_id for node in result] == [None, "a", "a", None, "d"]
    assert all(node.locator == original.locator for node, original in zip(result, nodes))


def test_build_hierarchy_rejects_mismatched_or_mixed_source_inputs() -> None:
    with pytest.raises(ValueError, match="same length"):
        build_hierarchy((make_node("a"),), ())

    other_source = Node(
        node_id="b",
        source_id="other",
        source_version="rev-1",
        kind="section",
        title="b",
        locator=NativeLocator("section", 1, {}),
    )
    with pytest.raises(ValueError, match="one source version"):
        build_hierarchy((make_node("a"), other_source), (1, 2))
