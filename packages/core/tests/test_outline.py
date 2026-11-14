from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import closing
from pathlib import Path
from types import MappingProxyType

import pytest

from dataindex.catalog import MAX_SOURCE_NODE_LIMIT, SQLiteCatalog
from dataindex.exceptions import SourceNotFoundError, StaleSourceVersionError
from dataindex.models import JsonValue, NativeLocator, Node, SourceRef
from dataindex.outline import (
    MAX_PROMPT_SUMMARY_CHARS,
    MAX_PROMPT_TITLE_CHARS,
    Outline,
    OutlineEntry,
    build_outline,
    limit_outline,
    outline_for_prompt,
    outline_tree,
)

SOURCE = SourceRef("doc-1", "text/markdown", "v1")


def lines(start: int, end: int) -> dict[str, JsonValue]:
    return {"line_start": start, "line_end": end}


def make_node(
    node_id: str,
    *,
    parent_id: str | None = None,
    title: str | None = None,
    value: Mapping[str, JsonValue] | None = None,
    summary: str | None = None,
    source: SourceRef = SOURCE,
) -> Node:
    return Node(
        node_id=node_id,
        source_id=source.source_id,
        source_version=source.version,
        kind="section",
        title=node_id if title is None else title,
        locator=NativeLocator("md-section", 1, {} if value is None else value),
        parent_id=parent_id,
        summary=summary,
    )


def index(catalog: SQLiteCatalog, *nodes: Node, source: SourceRef = SOURCE) -> SourceRef:
    catalog.create_source(source)
    for node in nodes:
        catalog.create_node(node)
    return source


def entry(
    node_id: str,
    depth: int,
    parent_id: str | None = None,
    *,
    title: str | None = None,
    value: Mapping[str, JsonValue] | None = None,
    summary: str | None = None,
) -> OutlineEntry:
    return OutlineEntry(
        node_id=node_id,
        parent_id=parent_id,
        depth=depth,
        kind="section",
        title=node_id if title is None else title,
        locator=NativeLocator("md-section", 1, {} if value is None else value),
        summary=summary,
    )


def sample_outline(*, truncated: bool = False) -> Outline:
    return Outline(
        SOURCE,
        (
            entry("A", 0),
            entry("A1", 1, "A"),
            entry("A1a", 2, "A1"),
            entry("A2", 1, "A"),
            entry("B", 0),
            entry("B1", 1, "B"),
            entry("B1a", 2, "B1"),
        ),
        truncated,
    )


def ids(outline: Outline) -> list[str]:
    return [item.node_id for item in outline.entries]


def assert_well_formed(outline: Outline) -> None:
    depths: dict[str, int] = {}
    for item in outline.entries:
        if item.parent_id is None:
            assert item.depth == 0
        else:
            assert item.parent_id in depths, "a parent must precede its children"
            assert item.depth == depths[item.parent_id] + 1
        assert item.node_id not in depths, "an entry must appear only once"
        depths[item.node_id] = item.depth


def tree_item(
    node_id: str, *children: object, summary: str | None = None
) -> dict[str, object]:
    item: dict[str, object] = {
        "node_id": node_id,
        "title": node_id,
        "kind": "section",
        "locator": {"scheme": "md-section", "version": 1, "value": {}},
    }
    if summary is not None:
        item["summary"] = summary
    item["nodes"] = list(children)
    return item


@pytest.fixture
def catalog() -> Iterator[SQLiteCatalog]:
    with SQLiteCatalog(":memory:") as opened:
        yield opened


def test_build_outline_nests_children_in_document_order(catalog: SQLiteCatalog) -> None:
    # Insertion order deliberately disagrees with document order.
    source = index(
        catalog,
        make_node("b", value=lines(20, 30)),
        make_node("a", value=lines(1, 19)),
        make_node("a2", parent_id="a", value=lines(10, 19)),
        make_node("a1", parent_id="a", value=lines(2, 9)),
        make_node("a1x", parent_id="a1", value=lines(3, 4)),
        make_node("b1", parent_id="b", value=lines(21, 30)),
    )

    outline = build_outline(catalog, source)

    assert [(item.node_id, item.parent_id, item.depth) for item in outline.entries] == [
        ("a", None, 0),
        ("a1", "a", 1),
        ("a1x", "a1", 2),
        ("a2", "a", 1),
        ("b", None, 0),
        ("b1", "b", 1),
    ]
    assert outline.source == source
    assert outline.truncated is False
    assert_well_formed(outline)


def test_build_outline_entries_carry_catalog_fields(catalog: SQLiteCatalog) -> None:
    locator = NativeLocator(
        "md-section", 2, {"line_start": 4, "line_end": 9, "heading_path": ["Guide"]}
    )
    node = Node(
        "n1", SOURCE.source_id, SOURCE.version, "section", "Install", locator,
        summary="How to install",
    )
    source = index(catalog, node)

    (item,) = build_outline(catalog, source).entries

    assert item == OutlineEntry(
        node_id="n1",
        parent_id=None,
        depth=0,
        kind="section",
        title="Install",
        locator=locator,
        summary="How to install",
    )


def test_build_outline_sorts_unlocated_nodes_last_then_by_title_and_id(
    catalog: SQLiteCatalog,
) -> None:
    source = index(
        catalog,
        make_node("z", title="Appendix"),
        make_node("y", title="Appendix"),
        # Booleans and strings are not line numbers.
        make_node("flag", title="Flag", value={"line_start": True}),
        make_node("text", title="Text", value={"line_start": "3"}),
        make_node("late", title="Late", value=lines(50, 60)),
        make_node("early", title="Early", value=lines(1, 5)),
        make_node("tie-b", title="Same", value=lines(10, 12)),
        make_node("tie-a", title="Same", value=lines(10, 12)),
    )

    assert ids(build_outline(catalog, source)) == [
        "early", "tie-a", "tie-b", "late", "y", "z", "flag", "text",
    ]


def test_build_outline_is_independent_of_insertion_order() -> None:
    def nodes() -> dict[str, Node]:
        return {
            "r1": make_node("r1", value=lines(1, 9)),
            "r2": make_node("r2", value=lines(10, 19)),
            "r3": make_node("r3", value=lines(20, 29)),
            "c1": make_node("c1", parent_id="r1", value=lines(2, 3)),
            "c2": make_node("c2", parent_id="r2", value=lines(11, 12)),
            "c3": make_node("c3", parent_id="r1", value=lines(4, 5)),
        }

    outlines = []
    for order in (
        ("r1", "r2", "r3", "c1", "c2", "c3"),
        ("r3", "r2", "r1", "c3", "c2", "c1"),
    ):
        with SQLiteCatalog(":memory:") as fresh:
            by_id = nodes()
            source = index(fresh, *(by_id[node_id] for node_id in order))
            outlines.append(build_outline(fresh, source))

    assert outlines[0] == outlines[1]
    assert ids(outlines[0]) == ["r1", "c1", "c3", "r2", "c2", "r3"]


def test_build_outline_of_a_source_without_nodes_is_empty(catalog: SQLiteCatalog) -> None:
    outline = build_outline(catalog, index(catalog))

    assert outline.entries == ()
    assert outline.truncated is False


def test_build_outline_ignores_nodes_of_other_sources(catalog: SQLiteCatalog) -> None:
    other = SourceRef("doc-2", "text/markdown", "v1")
    catalog.create_source(other)
    catalog.create_node(make_node("other-node", source=other))
    source = index(catalog, make_node("a"))

    assert ids(build_outline(catalog, source)) == ["a"]
    assert ids(build_outline(catalog, other)) == ["other-node"]


@pytest.mark.parametrize(
    ("max_nodes", "expected_ids", "truncated"),
    [
        (6, ["n1", "n2", "n3", "n4", "n5"], False),
        (5, ["n1", "n2", "n3", "n4", "n5"], False),
        (4, ["n1", "n2", "n3", "n4"], True),
        (1, ["n1"], True),
    ],
)
def test_build_outline_flags_truncation_only_when_nodes_were_dropped(
    catalog: SQLiteCatalog,
    max_nodes: int,
    expected_ids: list[str],
    truncated: bool,
) -> None:
    source = index(
        catalog,
        *(make_node(f"n{number}", value=lines(number, number)) for number in range(1, 6)),
    )

    outline = build_outline(catalog, source, max_nodes=max_nodes)

    assert ids(outline) == expected_ids
    assert outline.truncated is truncated


def test_build_outline_truncation_never_leaves_orphaned_entries(
    catalog: SQLiteCatalog,
) -> None:
    source = index(
        catalog,
        make_node("n1", value=lines(1, 2)),
        make_node("n2", value=lines(3, 4)),
        make_node("n3", parent_id="n2", value=lines(5, 6)),
    )
    # n1 now hangs under n3, which sits after it in insertion order.
    catalog.update_node(make_node("n1", parent_id="n3", value=lines(1, 2)))
    assert ids(build_outline(catalog, source)) == ["n2", "n3", "n1"]

    cut = build_outline(catalog, source, max_nodes=2)

    assert cut.truncated is True
    assert ids(cut) == ["n2"]
    assert_well_formed(cut)


def test_build_outline_terminates_on_a_corrupted_parent_cycle(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    with SQLiteCatalog(database) as catalog:
        source = index(
            catalog,
            make_node("root", value=lines(1, 2)),
            make_node("a", parent_id="root", value=lines(3, 4)),
            make_node("b", parent_id="a", value=lines(5, 6)),
        )
        # The catalog API refuses cycles, so corrupt the file directly.
        with closing(sqlite3.connect(database)) as raw:
            raw.execute("UPDATE nodes SET parent_id = 'b' WHERE node_id = 'a'")
            raw.commit()

        outline = build_outline(catalog, source)

    assert ids(outline) == ["root"]
    assert_well_formed(outline)


@pytest.mark.parametrize(
    "max_nodes", [0, -1, True, 1.5, "10", None, MAX_SOURCE_NODE_LIMIT]
)
def test_build_outline_rejects_invalid_node_caps(
    catalog: SQLiteCatalog, max_nodes: object
) -> None:
    source = index(catalog, make_node("a"))

    with pytest.raises(ValueError, match="max_nodes"):
        build_outline(catalog, source, max_nodes=max_nodes)  # type: ignore[arg-type]


def test_build_outline_accepts_the_largest_node_cap(catalog: SQLiteCatalog) -> None:
    source = index(catalog, make_node("a"))

    outline = build_outline(catalog, source, max_nodes=MAX_SOURCE_NODE_LIMIT - 1)

    assert ids(outline) == ["a"]


def test_build_outline_rejects_unknown_sources(catalog: SQLiteCatalog) -> None:
    with pytest.raises(SourceNotFoundError, match="not indexed"):
        build_outline(catalog, SOURCE)


def test_build_outline_only_follows_the_current_source_revision(
    catalog: SQLiteCatalog,
) -> None:
    old = index(catalog, make_node("old-node"))
    revised = SourceRef(old.source_id, old.source_type, "v2")
    catalog.update_source(revised)

    with pytest.raises(StaleSourceVersionError, match="different version"):
        build_outline(catalog, old)
    assert build_outline(catalog, revised).entries == ()

    catalog.create_node(make_node("new-node", source=revised))
    assert ids(build_outline(catalog, revised)) == ["new-node"]


@pytest.mark.parametrize(
    ("max_entries", "expected"),
    [
        (1, ["A"]),
        (2, ["A", "B"]),
        (3, ["A", "A1", "B"]),
        (4, ["A", "A1", "A2", "B"]),
        (5, ["A", "A1", "A2", "B", "B1"]),
        (6, ["A", "A1", "A1a", "A2", "B", "B1"]),
    ],
)
def test_limit_outline_keeps_shallow_levels_first_and_stays_nested(
    max_entries: int, expected: list[str]
) -> None:
    limited = limit_outline(sample_outline(), max_entries)

    assert ids(limited) == expected
    assert limited.truncated is True
    assert limited.source == SOURCE
    assert_well_formed(limited)


def test_limit_outline_returns_the_same_outline_when_it_fits() -> None:
    outline = sample_outline()
    flagged = sample_outline(truncated=True)

    assert limit_outline(outline, 7) is outline
    assert limit_outline(outline, 1000) is outline
    assert limit_outline(flagged, 7) is flagged
    assert flagged.truncated is True


@pytest.mark.parametrize("max_entries", [0, -1, True, 2.5, "3", None])
def test_limit_outline_rejects_invalid_limits(max_entries: object) -> None:
    with pytest.raises(ValueError, match="max_entries"):
        limit_outline(sample_outline(), max_entries)  # type: ignore[arg-type]


def test_outline_tree_nests_by_depth_and_omits_absent_summaries() -> None:
    outline = Outline(
        SOURCE,
        (
            entry("A", 0, summary="About A"),
            entry("A1", 1, "A"),
            entry("A1a", 2, "A1"),
            entry("A2", 1, "A"),
            entry("B", 0),
        ),
    )

    assert outline_tree(outline) == [
        tree_item(
            "A",
            tree_item("A1", tree_item("A1a")),
            tree_item("A2"),
            summary="About A",
        ),
        tree_item("B"),
    ]


def test_outline_tree_of_an_empty_outline_is_empty() -> None:
    assert outline_tree(Outline(SOURCE, ())) == []


def test_outline_tree_locators_are_plain_json() -> None:
    value = MappingProxyType(
        {
            "line_start": 1,
            "heading_path": ["Guide", "Setup"],
            "extra": MappingProxyType({"key": "v"}),
        }
    )
    tree = outline_tree(Outline(SOURCE, (entry("A", 0, value=value),)))

    assert json.loads(json.dumps(tree)) == tree
    assert tree == [
        {
            **tree_item("A"),
            "locator": {
                "scheme": "md-section",
                "version": 1,
                "value": {
                    "line_start": 1,
                    "heading_path": ["Guide", "Setup"],
                    "extra": {"key": "v"},
                },
            },
        }
    ]


def test_outline_for_prompt_lists_compact_rows_without_locator_details() -> None:
    locator_value: dict[str, JsonValue] = {
        "line_start": 3,
        "line_end": 9,
        "byte_offset": 1234,
        "note": "internal-locator-detail",
    }
    outline = Outline(
        SOURCE,
        (
            entry("A", 0, title="Intro", value=locator_value, summary="Overview"),
            entry("A1", 1, "A", title="Setup"),
        ),
    )

    prompt = outline_for_prompt(outline, 10)

    assert prompt == {
        "outline": [
            {
                "node_id": "A",
                "depth": 0,
                "kind": "section",
                "title": "Intro",
                "lines": [3, 9],
                "summary": "Overview",
            },
            {"node_id": "A1", "depth": 1, "kind": "section", "title": "Setup"},
        ],
        "outline_entries": 2,
        "outline_truncated": False,
    }
    assert "internal-locator-detail" not in json.dumps(prompt)
    assert "1234" not in json.dumps(prompt)


def test_outline_for_prompt_clips_long_titles_and_summaries() -> None:
    outline = Outline(SOURCE, (entry("A", 0, title="T" * 500, summary="S" * 500),))

    (row,) = outline_for_prompt(outline, 5)["outline"]  # type: ignore[misc]

    assert row["title"] == "T" * MAX_PROMPT_TITLE_CHARS
    assert row["summary"] == "S" * MAX_PROMPT_SUMMARY_CHARS


@pytest.mark.parametrize("summary", [None, ""])
def test_outline_for_prompt_omits_empty_summaries(summary: str | None) -> None:
    outline = Outline(SOURCE, (entry("A", 0, summary=summary),))

    (row,) = outline_for_prompt(outline, 5)["outline"]  # type: ignore[misc]

    assert "summary" not in row


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"line_start": 4},
        {"line_end": 9},
        {"line_start": True, "line_end": 9},
        {"line_start": 4, "line_end": False},
        {"line_start": "4", "line_end": "9"},
        {"line_start": 4.0, "line_end": 9.0},
        {"line_start": None, "line_end": None},
    ],
)
def test_outline_for_prompt_omits_lines_unless_both_bounds_are_integers(
    value: dict[str, JsonValue],
) -> None:
    outline = Outline(SOURCE, (entry("A", 0, value=value),))

    (row,) = outline_for_prompt(outline, 5)["outline"]  # type: ignore[misc]

    assert "lines" not in row


def test_outline_for_prompt_bounds_rows_and_reports_truncation() -> None:
    prompt = outline_for_prompt(sample_outline(), 4)

    assert [row["node_id"] for row in prompt["outline"]] == ["A", "A1", "A2", "B"]  # type: ignore[index, union-attr]
    assert prompt["outline_entries"] == 4
    assert prompt["outline_truncated"] is True


def test_outline_for_prompt_keeps_the_builders_truncation_flag() -> None:
    prompt = outline_for_prompt(sample_outline(truncated=True), 100)

    assert prompt["outline_entries"] == 7
    assert prompt["outline_truncated"] is True


@pytest.mark.parametrize("max_entries", [0, -4, False])
def test_outline_for_prompt_rejects_invalid_limits(max_entries: object) -> None:
    with pytest.raises(ValueError, match="max_entries"):
        outline_for_prompt(sample_outline(), max_entries)  # type: ignore[arg-type]


def test_catalog_outline_flows_into_prompt_rows(catalog: SQLiteCatalog) -> None:
    source = index(
        catalog,
        make_node("intro", title="Intro", value=lines(1, 8), summary="What this is"),
        make_node("usage", parent_id="intro", title="Usage", value=lines(3, 8)),
    )

    prompt = outline_for_prompt(build_outline(catalog, source), 10)

    assert prompt["outline"] == [
        {
            "node_id": "intro",
            "depth": 0,
            "kind": "section",
            "title": "Intro",
            "lines": [1, 8],
            "summary": "What this is",
        },
        {
            "node_id": "usage",
            "depth": 1,
            "kind": "section",
            "title": "Usage",
            "lines": [3, 8],
        },
    ]
