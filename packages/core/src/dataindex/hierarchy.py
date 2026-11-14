"""Format-neutral hierarchy assembly adapted from PageIndex's outline builder."""

from __future__ import annotations

from dataclasses import replace
from typing import Sequence

from dataindex.models import Node


def build_hierarchy(nodes: Sequence[Node], levels: Sequence[int]) -> tuple[Node, ...]:
    """Assign each ordered node to the nearest preceding node at a shallower level."""
    if len(nodes) != len(levels):
        raise ValueError("nodes and levels must have the same length")
    if not nodes:
        return ()

    source_versions = {(node.source_id, node.source_version) for node in nodes}
    if len(source_versions) != 1:
        raise ValueError("a hierarchy must contain one source version")
    if len({node.node_id for node in nodes}) != len(nodes):
        raise ValueError("node IDs must be unique within a hierarchy")
    if any(node.parent_id is not None for node in nodes):
        raise ValueError("input nodes must not already have a parent")
    if any(isinstance(level, bool) or not isinstance(level, int) or level < 1 for level in levels):
        raise ValueError("levels must be positive integers")

    stack: list[tuple[int, str]] = []
    result: list[Node] = []
    for node, level in zip(nodes, levels, strict=True):
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent_id = stack[-1][1] if stack else None
        result.append(replace(node, parent_id=parent_id))
        stack.append((level, node.node_id))
    return tuple(result)
