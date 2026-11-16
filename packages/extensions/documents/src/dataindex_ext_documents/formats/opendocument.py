"""Readers for OpenDocument text (.odt), spreadsheets (.ods) and presentations (.odp)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from dataindex_ext_documents.exceptions import (
    DocumentTooLargeError,
    MalformedDocumentError,
    ProtectedDocumentError,
)
from dataindex_ext_documents.formats._base import (
    MAX_SPREADSHEET_COLUMNS,
    Block,
    DocumentSection,
    Row,
    Sheet,
    Slide,
    TextBudget,
    flow_sections,
    plain_title,
    slide_sections,
    trim_cells,
    workbook_sections,
)
from dataindex_ext_documents.formats._safexml import parse_tree, stream_xml
from dataindex_ext_documents.formats._safezip import SafeZip
from dataindex_ext_documents.formats.office import (
    attribute,
    format_number,
    local_name,
    split_tag,
)

MAX_HEADING_LEVEL = 6
MAX_SPREADSHEET_ROWS = 1_048_576
OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
DRAW = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
PRESENTATION = "urn:oasis:names:tc:opendocument:xmlns:presentation:1.0"
SVG = "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
# Footnotes, comments, change history, alt text and generated indexes are not
# body text; including them would duplicate or misplace content.
_SKIPPED = frozenset(
    {
        (TEXT, "note"),
        (TEXT, "tracked-changes"),
        (TEXT, "table-of-content"),
        (OFFICE, "annotation"),
        (OFFICE, "forms"),
        (OFFICE, "scripts"),
        (DRAW, "page-thumbnail"),
        (SVG, "title"),
        (SVG, "desc"),
        (TABLE, "shapes"),
    }
)
_FRAMES = frozenset({(DRAW, "frame"), (DRAW, "custom-shape"), (DRAW, "rect")})
_MIMETYPES = {
    "text": b"application/vnd.oasis.opendocument.text",
    "spreadsheet": b"application/vnd.oasis.opendocument.spreadsheet",
    "presentation": b"application/vnd.oasis.opendocument.presentation",
}
_LABELS = {
    "text": "OpenDocument text",
    "spreadsheet": "OpenDocument spreadsheet",
    "presentation": "OpenDocument presentation",
}


def _int_attribute(attrs: dict[str, str], name: str, default: int = 1) -> int:
    try:
        return max(0, int(attribute(attrs, name) or default))
    except ValueError:
        return default


def _date_text(value: str | None) -> str | None:
    """An ISO date or date-time, written the way the Excel readers write it."""
    if value is None:
        return None
    date, separator, time = value.partition("T")
    if not separator:
        return value
    whole, _, fraction = time.partition(".")
    if whole == "00:00:00" and not fraction.strip("0"):
        return date
    return f"{date} {time}"


@dataclass(slots=True)
class _Table:
    name: str
    rows: list[Row] = field(default_factory=list)
    row_number: int = 0
    row_repeat: int = 1
    cells: list[str] = field(default_factory=list)
    cell_repeat: int = 1
    cell_value: str | None = None
    cell_paragraphs: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Page:
    name: str
    title: str = ""
    paragraphs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class _OdfHandler:
    """Streams content.xml; ``kind`` selects text, spreadsheet or presentation output."""

    def __init__(self, kind: str, label: str) -> None:
        self.kind = kind
        self.label = label
        self.budget = TextBudget()
        self.skip = 0
        self.in_body = False
        # Paragraph collection: text:p and text:h, flattened when nested in frames.
        self.paragraph: list[str] | None = None
        self.paragraph_nesting = 0
        self.heading: int | None = None
        self.presentation_class: list[str | None] = []
        # Tables, innermost last; only the outermost one keeps its own rows.
        self.tables: list[_Table] = []
        self.blocks: list[Block] = []
        self.sheets: list[Sheet] = []
        self.pages: list[_Page] = []
        self.in_notes = False

    # -- streaming callbacks ------------------------------------------------

    def start(self, tag: str, attrs: dict[str, str]) -> None:
        if self.skip:
            self.skip += 1
            return
        key = split_tag(tag)
        if key in _SKIPPED:
            self.skip = 1
            return
        namespace, name = key
        if not self.in_body:
            if namespace == OFFICE and name in _MIMETYPES:
                if name != self.kind:
                    raise MalformedDocumentError(f"{self.label} has no {self.kind} body")
                self.in_body = True
            return
        if key in _FRAMES:
            self.presentation_class.append(attribute(attrs, "class"))
        if namespace == TEXT and name in ("p", "h"):
            self._start_paragraph(name, attrs)
        elif self.paragraph is not None:
            if namespace == TEXT and name == "s":
                self.paragraph.append(" " * min(_int_attribute(attrs, "c"), 1000))
            elif namespace == TEXT and name == "tab":
                self.paragraph.append("\t")
            elif namespace == TEXT and name == "line-break":
                self.paragraph.append("\n")
        elif namespace == TABLE and name == "table":
            self.tables.append(_Table(attribute(attrs, "name") or ""))
        elif self.tables and namespace == TABLE and name == "table-row":
            self._start_row(attrs)
        elif (
            self.tables
            and namespace == TABLE
            and name in ("table-cell", "covered-table-cell")
        ):
            self._start_cell(attrs)
        elif namespace == DRAW and name == "page" and self.kind == "presentation":
            self.pages.append(_Page(attribute(attrs, "name") or ""))
        elif namespace == PRESENTATION and name == "notes" and self.pages:
            self.in_notes = True

    def data(self, text: str) -> None:
        if self.skip or self.paragraph is None:
            return
        # ODF collapses runs of white space in paragraph content (ODF 1.2, 6.1.2);
        # repeated spaces are written as text:s elements.
        self.paragraph.append(re.sub(r"[ \t\r\n]+", " ", text))

    def end(self, tag: str) -> None:
        if self.skip:
            self.skip -= 1
            return
        if not self.in_body:
            return
        key = split_tag(tag)
        namespace, name = key
        if namespace == OFFICE and name == self.kind:
            self.in_body = False
        elif namespace == TEXT and name in ("p", "h"):
            self._end_paragraph()
        elif (
            self.tables
            and namespace == TABLE
            and name in ("table-cell", "covered-table-cell")
        ):
            self._end_cell()
        elif self.tables and namespace == TABLE and name == "table-row":
            self._end_row()
        elif namespace == TABLE and name == "table" and self.tables:
            self._end_table()
        elif namespace == PRESENTATION and name == "notes":
            self.in_notes = False
        if key in _FRAMES and self.presentation_class:
            self.presentation_class.pop()

    # -- paragraphs ---------------------------------------------------------

    def _start_paragraph(self, name: str, attrs: dict[str, str]) -> None:
        if self.paragraph is not None:
            self.paragraph_nesting += 1
            self.paragraph.append("\n")
            return
        self.paragraph = []
        self.heading = None
        if name == "h":
            self.heading = min(max(_int_attribute(attrs, "outline-level"), 1), MAX_HEADING_LEVEL)

    def _end_paragraph(self) -> None:
        if self.paragraph_nesting:
            self.paragraph_nesting -= 1
            return
        text = "\n".join(
            line.strip() for line in "".join(self.paragraph or []).split("\n")
        ).strip()
        self.paragraph = None
        if self.tables:
            if text:
                self.tables[-1].cell_paragraphs.append(plain_title(text))
            return
        self.budget.add(text)
        if self.kind == "text":
            self.blocks.append(
                Block(len(self.blocks) + 1, text, self.heading if text else None)
            )
        elif self.kind == "presentation" and self.pages and text:
            page = self.pages[-1]
            if self.in_notes:
                page.notes.append(text)
                return
            page.paragraphs.append(text)
            role = next((value for value in reversed(self.presentation_class) if value), None)
            if role == "title" and not page.title:
                page.title = plain_title(text)

    # -- tables -------------------------------------------------------------

    def _start_row(self, attrs: dict[str, str]) -> None:
        table = self.tables[-1]
        table.cells = []
        table.row_repeat = max(1, _int_attribute(attrs, "number-rows-repeated"))

    def _start_cell(self, attrs: dict[str, str]) -> None:
        table = self.tables[-1]
        table.cell_paragraphs = []
        table.cell_repeat = max(1, _int_attribute(attrs, "number-columns-repeated"))
        value_type = attribute(attrs, "value-type")
        value: str | None = None
        if value_type == "float":
            raw = attribute(attrs, "value")
            try:
                value = format_number(float(raw)) if raw is not None else None
            except ValueError:
                value = raw
        elif value_type == "date":
            value = _date_text(attribute(attrs, "date-value"))
        elif value_type == "boolean":
            raw = (attribute(attrs, "boolean-value") or "").casefold()
            value = "TRUE" if raw == "true" else "FALSE" if raw == "false" else raw or None
        table.cell_value = value

    def _end_cell(self) -> None:
        table = self.tables[-1]
        text = table.cell_value or " ".join(table.cell_paragraphs)
        if text:
            if len(table.cells) + table.cell_repeat > MAX_SPREADSHEET_COLUMNS:
                raise DocumentTooLargeError(
                    f"{self.label} uses more than {MAX_SPREADSHEET_COLUMNS} columns"
                )
            for _ in range(table.cell_repeat):
                table.cells.append(self.budget.add(text))
        elif len(table.cells) + table.cell_repeat <= MAX_SPREADSHEET_COLUMNS:
            # Empty repeats are only materialized if a later cell has content.
            table.cells.extend([""] * table.cell_repeat)
        else:
            table.cells.append("")
        table.cell_value = None

    def _end_row(self) -> None:
        table = self.tables[-1]
        cells = trim_cells(table.cells)
        repeat = table.row_repeat
        if not any(cells):
            table.row_number += repeat
            return
        if table.row_number + repeat > MAX_SPREADSHEET_ROWS:
            raise DocumentTooLargeError(f"{self.label} has more than {MAX_SPREADSHEET_ROWS} rows")
        for copy in range(repeat):
            if copy:
                # The first copy's cells were counted when the cells ended.
                for cell in cells:
                    self.budget.add(cell)
            table.row_number += 1
            table.rows.append(Row(table.row_number, cells))

    def _end_table(self) -> None:
        table = self.tables.pop()
        if self.tables:
            # A nested table's rows become text of the enclosing cell.
            self.tables[-1].cell_paragraphs.extend(" | ".join(row.cells) for row in table.rows)
            return
        if self.kind == "spreadsheet":
            self.sheets.append(Sheet(table.name, len(self.sheets) + 1, tuple(table.rows)))
            return
        text = "\n".join(" | ".join(row.cells) for row in table.rows)
        if self.kind == "text":
            self.blocks.append(Block(len(self.blocks) + 1, text, kind="table"))
        elif self.kind == "presentation" and self.pages and text:
            target = self.pages[-1].notes if self.in_notes else self.pages[-1].paragraphs
            target.extend(" | ".join(row.cells) for row in table.rows)


def _read(raw: bytes, kind: str) -> _OdfHandler:
    label = _LABELS[kind]
    package = SafeZip(raw, label)
    mimetype = package.read_optional("mimetype", limit=256)
    if mimetype is None or mimetype.strip() != _MIMETYPES[kind]:
        raise MalformedDocumentError(f"{label} has a missing or different mimetype")
    manifest = package.read_optional("META-INF/manifest.xml")
    if manifest is not None:
        for entry in parse_tree(manifest, label=f"{label} manifest", budget=package.elements).iter():
            if local_name(entry.tag) != "file-entry":
                continue
            if attribute(entry.attrib, "full-path") == "content.xml" and any(
                local_name(child.tag) == "encryption-data" for child in entry
            ):
                raise ProtectedDocumentError(
                    f"{label} is password protected; remove the password and submit it again"
                )
    handler = _OdfHandler(kind, label)
    stream_xml(
        package.read("content.xml"), handler, label=label, budget=package.elements
    )
    return handler


def parse_odt(raw: bytes) -> tuple[DocumentSection, ...]:
    return flow_sections(_read(raw, "text").blocks, "odt")


def parse_ods(raw: bytes) -> tuple[DocumentSection, ...]:
    return workbook_sections(_read(raw, "spreadsheet").sheets, "ods")


def parse_odp(raw: bytes) -> tuple[DocumentSection, ...]:
    handler = _read(raw, "presentation")
    return slide_sections(
        (
            Slide(
                number,
                page.title,
                "\n".join(page.paragraphs),
                "\n".join(page.notes),
            )
            for number, page in enumerate(handler.pages, start=1)
        ),
        "odp",
    )
