"""Parsers must stay linear on hostile documents and keep the editor's line numbers."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from dataindex.models import SourceRef
from dataindex_ext_documents import DocumentTooLargeError
from dataindex_ext_documents.readers import (
    MAX_DOCUMENT_SECTIONS,
    MAX_TITLE_CHARS,
    IndexedDocument,
    parse_document,
)

CHILD_SCRIPT = """
from dataindex.models import SourceRef
from dataindex_ext_documents.exceptions import DocumentTooLargeError, EmptyDocumentError
from dataindex_ext_documents.readers import parse_document

try:
    parse_document(SourceRef("doc", {source_type!r}, "v1"), {content})
except (DocumentTooLargeError, EmptyDocumentError):
    pass
"""


def parse(source_type: str, content: str) -> IndexedDocument:
    return parse_document(SourceRef("doc", source_type, "v1"), content)


def lines_of(document: IndexedDocument) -> dict[str, tuple[int, int]]:
    return {
        section.title: (
            section.locator.value["line_start"],
            section.locator.value["line_end"],
        )
        for section in document.sections
    }


@pytest.mark.parametrize(
    ("line", "title", "level"),
    [
        ("# Title", "Title", 1),
        ("## Title ##", "Title", 2),
        ("### Title   ", "Title", 3),
        ("#\tTabbed", "Tabbed", 1),
        ("   # Indented by three", "Indented by three", 1),
        ("###### Deepest #", "Deepest", 6),
        ("# Title#", "Title", 1),
        ("#  ", "Untitled section", 1),
    ],
)
def test_atx_headings_keep_their_level_and_title(
    line: str, title: str, level: int
) -> None:
    document = parse("text/markdown", f"{line}\nbody\n")

    assert [(section.title, section.level) for section in document.sections] == [
        (title, level)
    ]


@pytest.mark.parametrize(
    "line", ["#", "# ", "####### Seven", "    # Four spaces", "#Tight"]
)
def test_lines_that_are_not_atx_headings_open_no_section(line: str) -> None:
    document = parse("text/markdown", f"{line}\nbody\n")

    assert [(section.kind, section.title) for section in document.sections] == [
        ("document", "Document")
    ]


def test_links_and_images_in_a_markdown_title_are_reduced_to_their_text() -> None:
    document = parse(
        "text/markdown",
        "# See [the docs](http://example.test/a) and ![logo](l.png) *now*\nbody\n",
    )

    assert document.sections[0].title == "See the docs and logo now"


def test_a_markdown_title_is_capped() -> None:
    document = parse("text/markdown", "# " + "word " * 1000 + "\nbody\n")

    assert 0 < len(document.sections[0].title) <= MAX_TITLE_CHARS


def test_only_standard_line_endings_count_as_markdown_lines() -> None:
    # Form feed, NEL and U+2028 do not end a line in editors or in CommonMark.
    text = "# One\x0c\n\nfirst\u2028line\x85 two\n\n## Two\nbody\n"

    document = parse("text/markdown", text)

    assert lines_of(document) == {"One": (1, 6), "Two": (5, 6)}
    assert "first\u2028line\x85 two" in document.sections[0].text


def test_only_standard_line_endings_count_as_plain_text_lines() -> None:
    document = parse("text/plain", "one\x0btwo\r\n\r\nthree\u2029four\rfive\n")

    assert [
        (node.locator.value["line_start"], node.locator.value["line_end"])
        for node in document.nodes
    ] == [(1, 1), (3, 4)]


def test_only_standard_line_endings_count_as_html_lines() -> None:
    document = parse("text/html", "<p>one\u2028two</p>\n<p>three</p>")

    assert document.sections[0].locator.value["line_end"] == 2


@pytest.mark.parametrize(
    "markup",
    [
        "<svg><g><title>x</title><text>y</svg><p>visible</p>",
        "<template><div></span>x</template><p>visible</p>",
        "<svg><svg></svg>hidden</svg><p>visible</p>",
        "<script>if (a < b) { x = '<p>no</p>' }</script><p>visible</p>",
    ],
    ids=[
        "an end tag closes the elements nested in it",
        "an unmatched end tag is ignored",
        "an end tag closes only the innermost match",
        "script text is never parsed as markup",
    ],
)
def test_ignored_elements_hide_their_content_until_they_are_closed(
    markup: str,
) -> None:
    document = parse("text/html", markup)

    assert [section.text for section in document.sections] == ["visible"]


def test_html_with_too_many_headings_is_refused() -> None:
    with pytest.raises(DocumentTooLargeError, match="section limit"):
        parse("text/html", "<h2>x</h2>" * (MAX_DOCUMENT_SECTIONS + 1))


@pytest.mark.parametrize(
    ("source_type", "content"),
    [
        ("text/markdown", '"# a" + " " * 200_000 + "b"'),
        ("text/markdown", '"# a" + "#" * 200_000 + "b"'),
        ("text/markdown", '"# " + "![" * 50_000'),
        ("text/markdown", '"# " + "[x](" * 50_000'),
        ("text/html", '"<svg>" + "<g>" * 150_000 + "</g>" * 150_000'),
        ("text/html", '"<svg>" + "<g>" * 100_000 + "</x>" * 100_000'),
        ("text/html", '"<h1>x" * 100_000'),
    ],
    ids=[
        "markdown heading with a long run of blanks",
        "markdown heading with a long run of hashes",
        "markdown title with unclosed image markup",
        "markdown title with unclosed link markup",
        "html with deeply nested ignored elements",
        "html with unmatched end tags inside an ignored element",
        "html with a hundred thousand headings",
    ],
)
def test_hostile_documents_are_parsed_or_refused_promptly(
    source_type: str, content: str
) -> None:
    # Each of these took minutes or hours before the parsers were made linear. A child
    # process lets a regression fail the test instead of hanging the whole suite.
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(path for path in sys.path if path),
    }
    script = CHILD_SCRIPT.format(source_type=source_type, content=content)

    try:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("the parser did not finish within 30 seconds")

    assert completed.returncode == 0, completed.stderr[-400:]
