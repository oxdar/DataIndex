"""Natural-boundary parsers for Markdown, plain text and HTML."""

from __future__ import annotations

import html
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from html.parser import HTMLParser

from dataindex.models import NativeLocator
from dataindex_ext_documents.exceptions import DocumentTooLargeError, EmptyDocumentError
from dataindex_ext_documents.formats._base import (
    MAX_DOCUMENT_EVENTS,
    MAX_DOCUMENT_SECTIONS,
    MAX_HTML_TAGS,
    MAX_TITLE_CHARS,
    DocumentSection,
    ElementBudget,
    check_line_limit,
    decode_utf8,
    line_text,
    plain_title,
    slug,
    source_lines,
)

_ATX_OPENING = re.compile(r"^ {0,3}(#{1,6})([ \t]+)")
_SETEXT_HEADING = re.compile(r"^ {0,3}(=+|-+)\s*$")
_FENCE_START = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _decode_text(raw: bytes) -> str:
    content = decode_utf8(raw)
    if not content.strip():
        raise EmptyDocumentError("document contains no indexable text")
    check_line_limit(content)
    return content


def parse_markdown(raw: bytes) -> tuple[DocumentSection, ...]:
    return _parse_markdown(_decode_text(raw))


def parse_plain_text(raw: bytes) -> tuple[DocumentSection, ...]:
    return _parse_plain_text(_decode_text(raw))


def parse_html(raw: bytes) -> tuple[DocumentSection, ...]:
    return parse_html_text(_decode_text(raw))


def parse_html_text(content: str) -> tuple[DocumentSection, ...]:
    return _html_sections(content, _html_events(content))


def html_visible_text(content: str) -> str:
    """All visible text of an HTML document, headings on their own lines."""
    return _visible_text(_html_events(content))


def parse_html_chapter(
    content: str, elements: ElementBudget
) -> tuple[str, tuple[DocumentSection, ...]]:
    """Visible text and sections of one HTML file of a larger document.

    One parse serves both, and ``elements`` caps the tags of all files together.
    """
    events = _html_events(content, elements)
    return _visible_text(events), _html_sections(content, events)


def _atx_heading(value: str) -> tuple[int, str] | None:
    """Return ``(level, raw title)`` for an ATX heading line, else ``None``.

    This is written as string operations rather than one regular expression because
    ``(.+?)\\s*#*\\s*$`` backtracks cubically on long runs of blanks or ``#``.
    """
    opening = _ATX_OPENING.match(value)
    if opening is None:
        return None
    marker, blanks = opening.groups()
    tail = value[opening.end() :]
    if not tail:
        return (len(marker), blanks[-1]) if len(blanks) > 1 else None
    end = len(tail.rstrip().rstrip("#").rstrip())
    return len(marker), tail[: max(end, 1)]


def _markdown_title(raw: str) -> str:
    # The cap bounds the link and image patterns below, which rescan the title once
    # per "[" or "![" and are quadratic on adversarial headings.
    title = html.unescape(raw.strip()[:MAX_TITLE_CHARS])
    title = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", title)
    title = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", title)
    return re.sub(r"[*_~`]", "", title).strip()


def _parse_markdown(content: str) -> tuple[DocumentSection, ...]:
    lines = source_lines(content)
    line_values = [line.rstrip("\r\n") for line in lines]
    in_fence = [False] * len(lines)
    fence: tuple[str, int] | None = None
    for index, value in enumerate(line_values):
        fence_match = _FENCE_START.match(value)
        if fence is None and fence_match:
            marker = fence_match.group(1)
            fence = (marker[0], len(marker))
            in_fence[index] = True
            continue
        if fence is not None:
            in_fence[index] = True
            close = re.match(r"^ {0,3}(`+|~+)\s*$", value)
            if close and close.group(1)[0] == fence[0] and len(close.group(1)) >= fence[1]:
                fence = None

    raw_headings: dict[int, tuple[int, str, int]] = {}
    for index, value in enumerate(line_values):
        if in_fence[index]:
            continue
        atx = _atx_heading(value)
        if atx is not None:
            raw_headings[index] = (atx[0], _markdown_title(atx[1]), index)
            if len(raw_headings) > MAX_DOCUMENT_SECTIONS:
                raise DocumentTooLargeError(
                    f"document exceeds the {MAX_DOCUMENT_SECTIONS}-section limit"
                )
            continue
        setext = _SETEXT_HEADING.match(value)
        if (
            setext
            and index > 0
            and not in_fence[index - 1]
            and line_values[index - 1].strip()
            and index - 1 not in raw_headings
        ):
            raw_headings[index - 1] = (
                1 if setext.group(1).startswith("=") else 2,
                _markdown_title(line_values[index - 1]),
                index,
            )
            if len(raw_headings) > MAX_DOCUMENT_SECTIONS:
                raise DocumentTooLargeError(
                    f"document exceeds the {MAX_DOCUMENT_SECTIONS}-section limit"
                )

    headings = sorted(
        (
            (start, level, title, underline)
            for start, (level, title, underline) in raw_headings.items()
        ),
        key=lambda item: item[0],
    )
    anchor_counts: Counter[str] = Counter()
    sections: list[DocumentSection] = []
    if headings and any(line.strip() for line in lines[: headings[0][0]]):
        preface_end = headings[0][0] - 1
        preface = line_text(lines, 0, preface_end)
        sections.append(
            DocumentSection(
                stable_key="preamble",
                title="Introduction",
                kind="document_intro",
                level=1,
                locator=NativeLocator(
                    "markdown-lines",
                    1,
                    {"line_start": 1, "line_end": preface_end + 1},
                ),
                search_text=preface,
                text=preface,
            )
        )
    if not headings:
        sections.append(
            DocumentSection(
                stable_key="document",
                title="Document",
                kind="document",
                level=1,
                locator=NativeLocator(
                    "markdown-lines",
                    1,
                    {"line_start": 1, "line_end": len(lines)},
                ),
                search_text=content,
                text=content,
            )
        )
        return tuple(sections)

    for position, (start, level, title, _) in enumerate(headings):
        end = len(lines) - 1
        direct_end = len(lines) - 1
        direct_end_set = False
        for later_start, later_level, _, _ in headings[position + 1 :]:
            if not direct_end_set:
                direct_end = later_start - 1
                direct_end_set = True
            if later_level <= level:
                end = later_start - 1
                break
        end = max(start, end)
        direct_end = max(start, direct_end)
        anchor_base = slug(title)
        duplicate = anchor_counts[anchor_base]
        anchor_counts[anchor_base] += 1
        anchor = anchor_base if duplicate == 0 else f"{anchor_base}-{duplicate}"
        section_text = line_text(lines, start, end)
        sections.append(
            DocumentSection(
                stable_key=f"heading:{anchor}",
                title=title or "Untitled section",
                kind="heading",
                level=level,
                locator=NativeLocator(
                    "markdown-heading",
                    1,
                    {
                        "anchor": anchor,
                        "level": level,
                        "line_start": start + 1,
                        "line_end": end + 1,
                    },
                ),
                search_text=line_text(lines, start, direct_end),
                text=section_text,
            )
        )
    return tuple(sections)


def _parse_plain_text(content: str) -> tuple[DocumentSection, ...]:
    lines = source_lines(content)
    sections: list[DocumentSection] = []
    start: int | None = None
    for index, line in enumerate(lines + [""]):
        if line.strip():
            if start is None:
                start = index
            continue
        if start is None:
            continue
        end = index - 1
        text = line_text(lines, start, end)
        title = plain_title(next(part for part in text.splitlines() if part.strip()))
        ordinal = len(sections) + 1
        sections.append(
            DocumentSection(
                stable_key=f"paragraph:{ordinal}",
                title=title[:120],
                kind="paragraph",
                level=1,
                locator=NativeLocator(
                    "text-paragraph",
                    1,
                    {
                        "paragraph": ordinal,
                        "line_start": start + 1,
                        "line_end": end + 1,
                    },
                ),
                search_text=text,
                text=text,
            )
        )
        if len(sections) > MAX_DOCUMENT_SECTIONS:
            raise DocumentTooLargeError(
                f"document exceeds the {MAX_DOCUMENT_SECTIONS}-section limit"
            )
        start = None
    return tuple(sections)


@dataclass(slots=True)
class _HtmlEvent:
    kind: str
    text: str
    line: int
    level: int = 0
    tag: str | None = None
    element_id: str | None = None


class _VisibleHTMLParser(HTMLParser):
    _IGNORED = frozenset(
        {"head", "script", "style", "template", "noscript", "iframe", "object", "svg", "math"}
    )
    _VOID = frozenset(
        {
            "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
            "meta", "param", "source", "track", "wbr",
        }
    )
    _BLOCK = frozenset(
        {
            "address", "article", "blockquote", "br", "dd", "div", "dl", "dt",
            "figcaption", "figure", "footer", "form", "header", "li", "main",
            "ol", "p", "pre", "section", "table", "tbody", "td", "th", "tr", "ul",
        }
    )

    def __init__(self, elements: ElementBudget | None = None) -> None:
        super().__init__(convert_charrefs=True)
        # Each tag costs parsing time even when it emits nothing.
        self.elements = elements or ElementBudget(MAX_HTML_TAGS)
        self.events: list[_HtmlEvent] = []
        self.ignored_tags: list[str] = []
        self._ignored_counts: Counter[str] = Counter()
        self.active_heading: _HtmlEvent | None = None

    def _emit(self, event: _HtmlEvent) -> None:
        if len(self.events) >= MAX_DOCUMENT_EVENTS:
            raise DocumentTooLargeError(
                f"HTML exceeds the {MAX_DOCUMENT_EVENTS}-event limit"
            )
        self.events.append(event)

    def _ignore(self, tag: str) -> None:
        self.ignored_tags.append(tag)
        self._ignored_counts[tag] += 1

    def _stop_ignoring(self, tag: str) -> None:
        # Close the innermost open ignored element named ``tag`` and everything nested
        # in it. The counts make an unmatched end tag O(1) instead of a stack scan.
        if not self._ignored_counts[tag]:
            return
        while True:
            popped = self.ignored_tags.pop()
            self._ignored_counts[popped] -= 1
            if popped == tag:
                return

    def _count_tag(self) -> None:
        elements = self.elements
        elements.used += 1
        if elements.used > elements.limit:
            raise DocumentTooLargeError(f"HTML has more than {elements.limit} tags")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._count_tag()
        tag = tag.lower()
        if self.ignored_tags:
            if tag not in self._VOID:
                self._ignore(tag)
            return
        if tag in self._IGNORED:
            self._ignore(tag)
            return
        heading = re.fullmatch(r"h([1-6])", tag)
        line = self.getpos()[0]
        if heading:
            attributes = dict(attrs)
            self.active_heading = _HtmlEvent(
                kind="heading",
                text="",
                line=line,
                level=int(heading.group(1)),
                tag=tag,
                element_id=attributes.get("id"),
            )
            self._emit(self.active_heading)
        elif tag in self._BLOCK:
            self._emit(_HtmlEvent("text", "\n", line))

    def handle_endtag(self, tag: str) -> None:
        self._count_tag()
        tag = tag.lower()
        if self.ignored_tags:
            self._stop_ignoring(tag)
            return
        if self.active_heading is not None and tag == self.active_heading.tag:
            self.active_heading = None
            self._emit(_HtmlEvent("text", "\n", self.getpos()[0]))
        elif tag in self._BLOCK:
            self._emit(_HtmlEvent("text", "\n", self.getpos()[0]))

    def handle_data(self, data: str) -> None:
        if self.ignored_tags:
            return
        if self.active_heading is not None:
            self.active_heading.text += data
        else:
            self._emit(_HtmlEvent("text", data, self.getpos()[0]))


def _normalize_visible(parts: Iterable[str]) -> str:
    text = "".join(parts)
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _html_events(
    content: str, elements: ElementBudget | None = None
) -> list[_HtmlEvent]:
    parser = _VisibleHTMLParser(elements)
    parser.feed(content)
    parser.close()
    return parser.events


def _visible_text(events: list[_HtmlEvent]) -> str:
    return _normalize_visible(
        event.text if event.kind == "text" else f"\n{event.text}\n" for event in events
    )


def _html_sections(
    content: str, events: list[_HtmlEvent]
) -> tuple[DocumentSection, ...]:
    headings = [
        (index, event)
        for index, event in enumerate(events)
        if event.kind == "heading"
    ]
    line_count = max(1, len(source_lines(content)))
    if len(headings) > MAX_DOCUMENT_SECTIONS:
        raise DocumentTooLargeError(
            f"document exceeds the {MAX_DOCUMENT_SECTIONS}-section limit"
        )
    if not headings:
        visible = _normalize_visible(event.text for event in events)
        if not visible:
            return ()
        return (
            DocumentSection(
                stable_key="document",
                title="Document",
                kind="document",
                level=1,
                locator=NativeLocator(
                    "html-document",
                    1,
                    {"line_start": 1, "line_end": line_count},
                ),
                search_text=visible,
                text=visible,
            ),
        )

    sections: list[DocumentSection] = []
    first_heading_index = headings[0][0]
    preface = _normalize_visible(
        event.text for event in events[:first_heading_index] if event.kind == "text"
    )
    if preface:
        first_heading_line = headings[0][1].line
        sections.append(
            DocumentSection(
                stable_key="preamble",
                title="Introduction",
                kind="document_intro",
                level=1,
                locator=NativeLocator(
                    "html-lines",
                    1,
                    {"line_start": 1, "line_end": max(1, first_heading_line - 1)},
                ),
                search_text=preface,
                text=preface,
            )
        )

    anchor_counts: Counter[str] = Counter()
    for position, (event_index, heading) in enumerate(headings):
        title = plain_title(heading.text) or f"Untitled {heading.tag}"
        end_index = len(events)
        direct_end_index = len(events)
        direct_end_set = False
        next_heading_line = line_count + 1
        for later_index, later_heading in headings[position + 1 :]:
            if not direct_end_set:
                direct_end_index = later_index
                direct_end_set = True
            if later_heading.level <= heading.level:
                end_index = later_index
                next_heading_line = later_heading.line
                break
        body = _normalize_visible(
            [
                heading.text,
                *(
                    event.text
                    for event in events[event_index + 1 : end_index]
                    if event.kind == "text"
                    or (event.kind == "heading" and event.text)
                ),
            ]
        )
        direct_body = _normalize_visible(
            [
                heading.text,
                *(
                    event.text
                    for event in events[event_index + 1 : direct_end_index]
                    if event.kind == "text"
                ),
            ]
        )
        base = slug(heading.element_id or title)
        occurrence = anchor_counts[base]
        anchor_counts[base] += 1
        anchor = base if occurrence == 0 else f"{base}-{occurrence}"
        line_end = max(heading.line, min(line_count, next_heading_line - 1))
        sections.append(
            DocumentSection(
                stable_key=f"heading:{anchor}",
                title=title,
                kind="heading",
                level=heading.level,
                locator=NativeLocator(
                    "html-heading",
                    1,
                    {
                        "anchor": heading.element_id,
                        "tag": heading.tag,
                        "heading": title,
                        "ordinal": position + 1,
                        "line_start": heading.line,
                        "line_end": line_end,
                    },
                ),
                search_text=direct_body,
                text=body,
            )
        )
    return tuple(sections)
