"""Section model, limits and section builders shared by every document reader.

Readers turn a file into native units (headed blocks, slides, pages, sheet rows,
JSON members, XML elements) and these builders turn the units into sections with
exact native locators. Nothing here splits text at arbitrary token counts: row
and item groups always end on a native row or item boundary.
"""

from __future__ import annotations

import io
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from dataindex.models import NativeLocator
from dataindex_ext_documents.exceptions import (
    DocumentTooLargeError,
    InvalidDocumentEncodingError,
)

MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
MAX_BINARY_DOCUMENT_BYTES = 50 * 1024 * 1024
MAX_EXTRACTED_CHARS = 5 * 1024 * 1024
MAX_DOCUMENT_SECTIONS = 10_000
MAX_DOCUMENT_EVENTS = 100_000
MAX_DOCUMENT_LINES = 100_000
MAX_TITLE_CHARS = 512
SHORT_TITLE_CHARS = 120
MAX_GROUP_ITEMS = 100
MAX_GROUP_CHARS = 16_000
MAX_SPREADSHEET_COLUMNS = 16_384
MAX_MARKUP_ELEMENTS = 2_000_000
MAX_HTML_TAGS = 1_000_000

_BOMS = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)


@dataclass(frozen=True, slots=True)
class DocumentSection:
    stable_key: str
    title: str
    kind: str
    level: int
    locator: NativeLocator
    search_text: str
    text: str


def too_many_sections() -> DocumentTooLargeError:
    return DocumentTooLargeError(
        f"document exceeds the {MAX_DOCUMENT_SECTIONS}-section limit"
    )


class ElementBudget:
    """Markup elements one document may make a reader visit, across all its parts.

    Parsing costs microseconds per element whether or not the element holds any
    text, so without a shared cap a small compressed upload made of millions of
    empty elements, spread over many parts, would keep a worker busy for minutes.
    """

    __slots__ = ("limit", "used")

    def __init__(self, limit: int = MAX_MARKUP_ELEMENTS) -> None:
        self.limit = limit
        self.used = 0


class TextBudget:
    """Counts extracted characters so a small container cannot expand unbounded."""

    __slots__ = ("used",)

    def __init__(self) -> None:
        self.used = 0

    def add(self, text: str) -> str:
        self.used += len(text)
        if self.used > MAX_EXTRACTED_CHARS:
            raise DocumentTooLargeError(
                f"document text exceeds the {MAX_EXTRACTED_CHARS}-character "
                "extraction limit"
            )
        return text


def decode_utf8(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InvalidDocumentEncodingError("document must be valid UTF-8") from error


def decode_text_with_bom(raw: bytes) -> str:
    """Decode UTF-8 (with or without a byte-order mark) or UTF-16 with a BOM.

    Legacy code pages are refused instead of guessed: a wrong guess would put
    silently corrupted text into cited evidence.
    """
    codec = next((name for bom, name in _BOMS if raw.startswith(bom)), "utf-8")
    try:
        return raw.decode(codec)
    except UnicodeDecodeError as error:
        raise InvalidDocumentEncodingError(
            "document must be UTF-8, or UTF-16 with a byte-order mark; re-save it "
            "with UTF-8 encoding"
        ) from error


def source_lines(content: str) -> list[str]:
    # Only \n, \r and \r\n end a line, as in editors and CommonMark. str.splitlines()
    # also splits on \v, \f, \x1c-\x1e, \x85, U+2028 and U+2029, which would shift the
    # cited line numbers and defeat the line limits.
    return io.StringIO(content, newline="").readlines()


def line_text(lines: list[str], start: int, end: int) -> str:
    return "".join(lines[start : end + 1])


def check_line_limit(content: str) -> None:
    line_breaks = content.count("\n") + content.count("\r") - content.count("\r\n")
    if line_breaks + 1 > MAX_DOCUMENT_LINES:
        raise DocumentTooLargeError(
            f"document exceeds the {MAX_DOCUMENT_LINES}-line limit"
        )


def plain_title(text: str) -> str:
    # Same result as collapsing r"\s+" runs and stripping, without a regex.
    return " ".join(text.split())


def short_title(text: str, limit: int = SHORT_TITLE_CHARS) -> str:
    first = next((line for line in text.splitlines() if line.strip()), "")
    return plain_title(first)[:limit]


def slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    result = re.sub(r"[^\w -]", "", normalized, flags=re.UNICODE)
    result = re.sub(r"[\s-]+", "-", result).strip("-")
    return result or "section"


class AnchorNames:
    """Unique, readable anchors: ``payment-terms``, ``payment-terms-1``, ..."""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()

    def __call__(self, title: str) -> str:
        base = slug(title[:MAX_TITLE_CHARS])
        occurrence = self._counts[base]
        self._counts[base] += 1
        return base if occurrence == 0 else f"{base}-{occurrence}"


def column_letters(column: int) -> str:
    """Spreadsheet column name for a 1-based index: 1 -> A, 27 -> AA."""
    letters = ""
    while column > 0:
        column, remainder = divmod(column - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def check_section_count(count: int) -> None:
    if count > MAX_DOCUMENT_SECTIONS:
        raise too_many_sections()


# ---------------------------------------------------------------------------
# Flow documents: word processors, RTF, e-mail bodies


@dataclass(frozen=True, slots=True)
class Block:
    """One top-level paragraph or table, numbered in document order from 1.

    Empty blocks keep their ordinal so a locator names the same block a user sees
    when counting paragraphs and tables in the source application.
    """

    ordinal: int
    text: str
    heading_level: int | None = None
    kind: str = "paragraph"


def _join_blocks(blocks: Sequence[Block]) -> str:
    return "\n".join(block.text for block in blocks)


def flow_sections(blocks: Iterable[Block], scheme: str) -> tuple[DocumentSection, ...]:
    """Group headed blocks like Markdown headings; otherwise one section per block."""
    content = [block for block in blocks if block.text.strip()]
    headings = [
        index for index, block in enumerate(content) if block.heading_level is not None
    ]
    if not headings:
        check_section_count(len(content))
        return tuple(
            DocumentSection(
                stable_key=f"block:{block.ordinal}",
                title=short_title(block.text) or f"Block {block.ordinal}",
                kind=block.kind,
                level=1,
                locator=NativeLocator(f"{scheme}-block", 1, {"block": block.ordinal}),
                search_text=block.text,
                text=block.text,
            )
            for block in content
        )
    check_section_count(len(headings) + 1)
    sections: list[DocumentSection] = []
    first = headings[0]
    if first > 0:
        intro = content[:first]
        intro_text = _join_blocks(intro)
        sections.append(
            DocumentSection(
                stable_key="preamble",
                title="Introduction",
                kind="document_intro",
                level=content[first].heading_level or 1,
                locator=NativeLocator(
                    f"{scheme}-blocks",
                    1,
                    {"block_start": intro[0].ordinal, "block_end": intro[-1].ordinal},
                ),
                search_text=intro_text,
                text=intro_text,
            )
        )
    anchors = AnchorNames()
    for position, index in enumerate(headings):
        heading = content[index]
        level = heading.heading_level or 1
        direct_end = (
            headings[position + 1] if position + 1 < len(headings) else len(content)
        )
        end = len(content)
        for later in headings[position + 1 :]:
            if (content[later].heading_level or 1) <= level:
                end = later
                break
        title = plain_title(heading.text)[:MAX_TITLE_CHARS] or "Untitled section"
        anchor = anchors(title)
        sections.append(
            DocumentSection(
                stable_key=f"heading:{anchor}",
                title=title,
                kind="heading",
                level=level,
                locator=NativeLocator(
                    f"{scheme}-heading",
                    1,
                    {
                        "anchor": anchor,
                        "level": level,
                        "block_start": heading.ordinal,
                        "block_end": content[end - 1].ordinal,
                    },
                ),
                search_text=_join_blocks(content[index:direct_end]),
                text=_join_blocks(content[index:end]),
            )
        )
    return tuple(sections)


# ---------------------------------------------------------------------------
# Presentations


@dataclass(frozen=True, slots=True)
class Slide:
    number: int
    title: str
    body: str
    notes: str = ""


def slide_sections(slides: Iterable[Slide], scheme: str) -> tuple[DocumentSection, ...]:
    sections: list[DocumentSection] = []
    for slide in slides:
        body = slide.body.strip()
        notes = slide.notes.strip()
        if not body and not notes:
            continue
        heading = plain_title(slide.title)[:SHORT_TITLE_CHARS] or short_title(
            body or notes
        )
        text = body + (f"\n\nSpeaker notes:\n{notes}" if notes else "")
        sections.append(
            DocumentSection(
                stable_key=f"slide:{slide.number}",
                title=(
                    f"Slide {slide.number}: {heading}"
                    if heading
                    else f"Slide {slide.number}"
                ),
                kind="slide",
                level=1,
                locator=NativeLocator(f"{scheme}-slide", 1, {"slide": slide.number}),
                search_text=text.strip(),
                text=text.strip(),
            )
        )
        check_section_count(len(sections))
    return tuple(sections)


# ---------------------------------------------------------------------------
# Tables: CSV, TSV and spreadsheet sheets


@dataclass(frozen=True, slots=True)
class Row:
    """One native row. ``number`` is the 1-based row (or CSV record) number."""

    number: int
    cells: tuple[str, ...]
    line_start: int | None = None
    line_end: int | None = None


@dataclass(frozen=True, slots=True)
class Sheet:
    name: str
    index: int
    rows: tuple[Row, ...]


def trim_cells(cells: Iterable[str]) -> tuple[str, ...]:
    values = list(cells)
    while values and not values[-1].strip():
        values.pop()
    return tuple(values)


def _row_line(row: Row) -> str:
    return f"Row {row.number}: " + " | ".join(row.cells)


def _row_label(row: Row) -> str:
    first = next((cell for cell in row.cells if cell.strip()), "")
    return plain_title(first)[:40]


@dataclass(frozen=True, slots=True)
class _RowGroup:
    rows: Sequence[Row]
    body: str


def _group_rows(rows: Sequence[Row]) -> list[_RowGroup]:
    """Consecutive rows, at most MAX_GROUP_ITEMS rows or ~MAX_GROUP_CHARS per group."""
    groups: list[_RowGroup] = []
    lines: list[str] = []
    start = size = 0
    for index, row in enumerate(rows):
        line = _row_line(row)
        if lines and (
            len(lines) >= MAX_GROUP_ITEMS or size + len(line) + 1 > MAX_GROUP_CHARS
        ):
            groups.append(_RowGroup(rows[start:index], "\n".join(lines)))
            check_section_count(len(groups))
            start, lines, size = index, [], 0
        lines.append(line)
        size += len(line) + 1
    if lines:
        groups.append(_RowGroup(rows[start:], "\n".join(lines)))
    return groups


def _rows_title(group: Sequence[Row]) -> str:
    first, last = group[0], group[-1]
    span = (
        f"Row {first.number}"
        if first is last
        else f"Rows {first.number}-{last.number}"
    )
    labels = [label for label in (_row_label(first), _row_label(last)) if label]
    if len(labels) == 2 and labels[0] != labels[1] and first is not last:
        return f"{span}: {labels[0]} … {labels[1]}"
    return f"{span}: {labels[0]}" if labels else span


def table_sections(rows: Iterable[Row], scheme: str) -> tuple[DocumentSection, ...]:
    """Sections for one delimited table: groups of rows under a repeated header."""
    content = [row for row in rows if any(cell.strip() for cell in row.cells)]
    if not content:
        return ()
    header, data = content[0], content[1:]
    header_line = f"Columns (row {header.number}): " + " | ".join(header.cells)
    groups = _group_rows(data) or [_RowGroup((header,), "")]
    check_section_count(len(groups))
    sections: list[DocumentSection] = []
    for group in groups:
        first, last = group.rows[0], group.rows[-1]
        text = "\n".join(filter(None, (header_line, group.body)))
        value: dict[str, int] = {"row_start": first.number, "row_end": last.number}
        if first.line_start is not None and last.line_end is not None:
            value["line_start"] = first.line_start
            value["line_end"] = last.line_end
        sections.append(
            DocumentSection(
                stable_key=f"rows:{first.number}-{last.number}",
                title=_rows_title(group.rows),
                kind="rows",
                level=1,
                locator=NativeLocator(f"{scheme}-rows", 1, value),
                search_text=text,
                text=text,
            )
        )
    return tuple(sections)


def workbook_sections(
    sheets: Iterable[Sheet], scheme: str
) -> tuple[DocumentSection, ...]:
    """One section per sheet; large sheets get row-group children."""
    sections: list[DocumentSection] = []
    for sheet in sheets:
        content = [row for row in sheet.rows if any(cell.strip() for cell in row.cells)]
        if not content:
            continue
        header, data = content[0], content[1:]
        header_line = f"Columns (row {header.number}): " + " | ".join(header.cells)
        width = max(1, max(len(row.cells) for row in content))
        sheet_range = f"A{header.number}:{column_letters(width)}{content[-1].number}"
        groups = _group_rows(data)
        title = plain_title(sheet.name)[:MAX_TITLE_CHARS] or f"Sheet {sheet.index}"
        locator = NativeLocator(
            f"{scheme}-sheet",
            1,
            {"sheet": sheet.name, "sheet_index": sheet.index, "range": sheet_range},
        )
        if len(groups) <= 1:
            text = "\n".join(
                [f"Sheet: {sheet.name}", header_line, *(group.body for group in groups)]
            )
            sections.append(
                DocumentSection(
                    f"sheet:{sheet.index}", title, "sheet", 1, locator, text, text
                )
            )
            check_section_count(len(sections))
            continue
        summary = (
            f"Sheet: {sheet.name}\n{len(data)} data rows in {len(groups)} row groups, "
            f"{width} columns\n{header_line}"
        )
        sections.append(
            DocumentSection(
                f"sheet:{sheet.index}", title, "sheet", 1, locator, summary, summary
            )
        )
        check_section_count(len(sections) + len(groups))
        for group in groups:
            first, last = group.rows[0], group.rows[-1]
            group_width = max(1, max(len(row.cells) for row in group.rows))
            text = f"{header_line}\n{group.body}"
            sections.append(
                DocumentSection(
                    stable_key=f"sheet:{sheet.index}:rows:{first.number}-{last.number}",
                    title=_rows_title(group.rows),
                    kind="rows",
                    level=2,
                    locator=NativeLocator(
                        f"{scheme}-rows",
                        1,
                        {
                            "sheet": sheet.name,
                            "sheet_index": sheet.index,
                            "row_start": first.number,
                            "row_end": last.number,
                            "range": (
                                f"A{first.number}:"
                                f"{column_letters(group_width)}{last.number}"
                            ),
                        },
                    ),
                    search_text=text,
                    text=text,
                )
            )
    return tuple(sections)
