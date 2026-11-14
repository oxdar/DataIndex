"""Bounded, document-ordered outlines of indexed sources.

Outlines are built only from catalog nodes (titles, kinds, native locators and
optional summaries). Source content is never read, so an outline is cheap to
cache and safe to show to a planner as untrusted data.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TypeGuard

from dataindex._json import locator_json
from dataindex.catalog import MAX_SOURCE_NODE_LIMIT, SQLiteCatalog
from dataindex.exceptions import SourceNotFoundError, StaleSourceVersionError
from dataindex.models import JsonValue, NativeLocator, Node, SourceRef

DEFAULT_MAX_OUTLINE_NODES = 10_000
MAX_PROMPT_TITLE_CHARS = 160
MAX_PROMPT_SUMMARY_CHARS = 240


@dataclass(frozen=True, slots=True)
class OutlineEntry:
    node_id: str
    parent_id: str | None
    depth: int
    kind: str
    title: str
    locator: NativeLocator
    summary: str | None = None


@dataclass(frozen=True, slots=True)
class Outline:
    """Depth-first, document-ordered entries for one pinned source version."""

    source: SourceRef
    entries: tuple[OutlineEntry, ...]
    truncated: bool = False


def build_outline(
    catalog: SQLiteCatalog,
    source: SourceRef,
    *,
    max_nodes: int = DEFAULT_MAX_OUTLINE_NODES,
) -> Outline:
    """Read the catalog tree of ``source`` in document order.

    ``truncated`` is set when more than ``max_nodes`` nodes are indexed. Nodes
    are kept in insertion order, so the kept set is a parent-closed prefix.
    """
    if (
        isinstance(max_nodes, bool)
        or not isinstance(max_nodes, int)
        or not 1 <= max_nodes < MAX_SOURCE_NODE_LIMIT
    ):
        raise ValueError(
            f"max_nodes must be between 1 and {MAX_SOURCE_NODE_LIMIT - 1}"
        )
    current = catalog.get_source(source.source_id)
    if current is None:
        raise SourceNotFoundError(f"source {source.source_id!r} is not indexed")
    if current.version != source.version:
        raise StaleSourceVersionError(
            f"source {source.source_id!r} is indexed at a different version"
        )
    listed = catalog.list_source_nodes(source.source_id, limit=max_nodes + 1)
    truncated = len(listed) > max_nodes
    children: dict[str | None, list[Node]] = {}
    for node in listed[:max_nodes]:
        children.setdefault(node.parent_id, []).append(node)
    for siblings in children.values():
        siblings.sort(key=_document_order)
    entries: list[OutlineEntry] = []
    pending = [(node, 0) for node in reversed(children.get(None, ()))]
    while pending:
        node, depth = pending.pop()
        entries.append(
            OutlineEntry(
                node_id=node.node_id,
                parent_id=node.parent_id,
                depth=depth,
                kind=node.kind,
                title=node.title,
                locator=node.locator,
                summary=node.summary,
            )
        )
        pending.extend(
            (child, depth + 1) for child in reversed(children.get(node.node_id, ()))
        )
    return Outline(source=source, entries=tuple(entries), truncated=truncated)


def limit_outline(outline: Outline, max_entries: int) -> Outline:
    """Keep the shallowest levels that fit, then fill the rest in document order.

    Every kept entry keeps its parent, so the result still nests correctly.
    """
    if (
        isinstance(max_entries, bool)
        or not isinstance(max_entries, int)
        or max_entries < 1
    ):
        raise ValueError("max_entries must be a positive integer")
    entries = outline.entries
    if len(entries) <= max_entries:
        return outline
    per_depth: dict[int, int] = {}
    for entry in entries:
        per_depth[entry.depth] = per_depth.get(entry.depth, 0) + 1
    kept_depth = -1
    total = 0
    for depth in sorted(per_depth):
        if total + per_depth[depth] > max_entries:
            break
        total += per_depth[depth]
        kept_depth = depth
    fill_depth = kept_depth + 1 if kept_depth >= 0 else min(per_depth)
    remaining = max_entries - total
    kept: set[str] = {
        entry.node_id for entry in entries if entry.depth <= kept_depth
    }
    for entry in entries:
        if remaining <= 0:
            break
        if entry.depth == fill_depth:
            kept.add(entry.node_id)
            remaining -= 1
    limited = tuple(entry for entry in entries if entry.node_id in kept)
    return Outline(source=outline.source, entries=limited, truncated=True)


def outline_tree(outline: Outline) -> list[JsonValue]:
    """Nest outline entries into ``{node_id, title, kind, locator, nodes}`` dicts."""
    roots: list[JsonValue] = []
    stack: list[tuple[int, list[JsonValue]]] = []
    for entry in outline.entries:
        children: list[JsonValue] = []
        item: dict[str, JsonValue] = {
            "node_id": entry.node_id,
            "title": entry.title,
            "kind": entry.kind,
            "locator": locator_json(entry.locator),
        }
        if entry.summary is not None:
            item["summary"] = entry.summary
        item["nodes"] = children
        while stack and stack[-1][0] >= entry.depth:
            stack.pop()
        siblings = stack[-1][1] if stack else roots
        siblings.append(item)
        stack.append((entry.depth, children))
    return roots


def outline_for_prompt(outline: Outline, max_entries: int) -> dict[str, JsonValue]:
    """Compact outline details for a planner prompt, bounded to ``max_entries``."""
    limited = limit_outline(outline, max_entries)
    rows: list[JsonValue] = []
    for entry in limited.entries:
        row: dict[str, JsonValue] = {
            "node_id": entry.node_id,
            "depth": entry.depth,
            "kind": entry.kind,
            "title": entry.title[:MAX_PROMPT_TITLE_CHARS],
        }
        span = _line_span(entry.locator)
        if span is not None:
            row["lines"] = span
        if entry.summary:
            row["summary"] = entry.summary[:MAX_PROMPT_SUMMARY_CHARS]
        rows.append(row)
    return {
        "outline": rows,
        "outline_entries": len(limited.entries),
        "outline_truncated": limited.truncated,
    }


def _document_order(node: Node) -> tuple[int, str, str]:
    start = node.locator.value.get("line_start")
    position = start if _is_int(start) else sys.maxsize
    return position, node.title, node.node_id


def _line_span(locator: NativeLocator) -> list[int] | None:
    start = locator.value.get("line_start")
    end = locator.value.get("line_end")
    if _is_int(start) and _is_int(end):
        return [start, end]
    return None


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)
