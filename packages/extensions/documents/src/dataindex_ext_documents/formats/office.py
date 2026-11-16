"""Readers for Office Open XML: Word (.docx), Excel (.xlsx), PowerPoint (.pptx).

Parts are located through the package relationships rather than fixed names and
streamed through the hardened expat parser. ``mc:Fallback`` content is skipped
because it repeats the preferred ``mc:Choice`` content in an older markup.
"""

from __future__ import annotations

import datetime as dt
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
    column_letters,
    flow_sections,
    plain_title,
    slide_sections,
    trim_cells,
    workbook_sections,
)
from dataindex_ext_documents.formats._safexml import parse_tree, stream_xml
from dataindex_ext_documents.formats._safezip import SafeZip, resolve_part

MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_RELATIONSHIP_ID_NS = frozenset(
    {
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        "http://purl.oclc.org/ooxml/officeDocument/relationships",
    }
)
MAX_HEADING_LEVEL = 6
MAX_PADDED_COLUMNS = 1024
_BUILTIN_DATE_FORMATS = frozenset(
    [*range(14, 23), *range(27, 37), *range(45, 48), *range(50, 59)]
)
_CELL_REFERENCE = re.compile(r"([A-Za-z]{1,3})(\d{1,7})$")
_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# An OLE directory names its streams in UTF-16LE.
_ENCRYPTED_PACKAGE_STREAM = "EncryptedPackage".encode("utf-16-le")


def split_tag(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        namespace, _, local = tag[1:].partition("}")
        return namespace, local
    return "", tag


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def attribute(attrs: dict[str, str], name: str) -> str | None:
    """Attribute by local name, whatever namespace prefix the producer chose."""
    for key, value in attrs.items():
        if local_name(key) == name:
            return value
    return None


def relationship_id(attrs: dict[str, str]) -> str | None:
    for key, value in attrs.items():
        namespace, local = split_tag(key)
        if local == "id" and namespace in _RELATIONSHIP_ID_NS:
            return value
    return None


class SkippingHandler:
    """Streaming handler base that ignores ``mc:Fallback`` and chosen subtrees."""

    skipped: frozenset[str] = frozenset()

    def __init__(self) -> None:
        self._skip = 0

    def start(self, tag: str, attrs: dict[str, str]) -> None:
        if self._skip:
            self._skip += 1
            return
        namespace, local = split_tag(tag)
        if (namespace == MC_NS and local == "Fallback") or local in self.skipped:
            self._skip = 1
            return
        self.on_start(local, attrs)

    def end(self, tag: str) -> None:
        if self._skip:
            self._skip -= 1
            return
        self.on_end(local_name(tag))

    def data(self, text: str) -> None:
        if not self._skip:
            self.on_data(text)

    def on_start(self, name: str, attrs: dict[str, str]) -> None: ...

    def on_end(self, name: str) -> None: ...

    def on_data(self, text: str) -> None: ...


@dataclass(frozen=True, slots=True)
class Relationship:
    id: str
    type: str
    part: str | None


def part_relationships(package: SafeZip, part: str) -> dict[str, Relationship]:
    directory, _, name = part.rpartition("/")
    rels_name = f"{directory}/_rels/{name}.rels" if directory else f"_rels/{name}.rels"
    data = package.read_optional(rels_name)
    if data is None:
        return {}
    relationships: dict[str, Relationship] = {}
    for element in parse_tree(data, label=f"{package.label} relationships", budget=package.elements).iter():
        if local_name(element.tag) != "Relationship":
            continue
        identifier = element.get("Id") or ""
        target = element.get("Target") or ""
        external = (element.get("TargetMode") or "").casefold() == "external"
        relationships[identifier] = Relationship(
            identifier,
            element.get("Type") or "",
            None if external else resolve_part(part, target),
        )
    return relationships


def related_parts(relationships: dict[str, Relationship], kind: str) -> list[str]:
    return [
        relationship.part
        for relationship in relationships.values()
        if relationship.part and relationship.type.rsplit("/", 1)[-1] == kind
    ]


def open_package(raw: bytes, label: str) -> SafeZip:
    """Open an OOXML package, explaining the OLE files that Office also writes.

    Office saves a password-protected .docx, .xlsx or .pptx as an OLE file holding
    an ``EncryptedPackage`` stream, and a 97-2003 file may carry a newer suffix.
    """
    if raw.startswith(_OLE_SIGNATURE):
        if _ENCRYPTED_PACKAGE_STREAM in raw:
            raise ProtectedDocumentError(
                f"{label} is password protected; remove the password and submit it again"
            )
        raise MalformedDocumentError(
            f"{label} is in the 97-2003 binary format; submit it with its 97-2003 "
            "extension (.doc, .xls or .ppt)"
        )
    return SafeZip(raw, label)


def main_part(package: SafeZip, description: str) -> str:
    """The package's main part; each reader then checks its root element."""
    candidates = related_parts(part_relationships(package, ""), "officeDocument")
    if not candidates or not package.has(candidates[0]):
        raise MalformedDocumentError(
            f"{package.label} has no main document part; it may not be {description}"
        )
    return candidates[0]


# ---------------------------------------------------------------------------
# Word


@dataclass(slots=True)
class _Style:
    name: str = ""
    based_on: str | None = None
    outline: int | None = None


def _style_levels(package: SafeZip, styles_part: str | None) -> dict[str, int]:
    if styles_part is None or not package.has(styles_part):
        return {}
    styles: dict[str, _Style] = {}
    tree = parse_tree(package.read(styles_part), label=f"{package.label} styles", budget=package.elements)
    for element in tree:
        if local_name(element.tag) != "style":
            continue
        if attribute(element.attrib, "type") not in (None, "paragraph"):
            continue
        style_id = attribute(element.attrib, "styleId")
        if not style_id:
            continue
        style = _Style()
        for child in element.iter():
            name = local_name(child.tag)
            if name == "name":
                style.name = (attribute(child.attrib, "val") or "").strip().casefold()
            elif name == "basedOn":
                style.based_on = attribute(child.attrib, "val")
            elif name == "outlineLvl":
                style.outline = _outline_level(attribute(child.attrib, "val"))
        styles[style_id] = style

    def level(style_id: str, seen: frozenset[str] = frozenset()) -> int | None:
        style = styles.get(style_id)
        if style is None or style_id in seen:
            return None
        heading = re.fullmatch(r"heading\s*([1-9])", style.name)
        if heading:
            return int(heading.group(1))
        if style.name == "title":
            return 1
        if style.outline is not None:
            return style.outline
        if style.based_on:
            return level(style.based_on, seen | {style_id})
        return None

    levels: dict[str, int] = {}
    for style_id in styles:
        found = level(style_id)
        if found is not None:
            levels[style_id] = min(found, MAX_HEADING_LEVEL)
    return levels


def _outline_level(value: str | None) -> int | None:
    # Word stores outline levels 0-8 for headings and 9 for body text.
    try:
        number = int(value or "")
    except ValueError:
        return None
    return number + 1 if 0 <= number <= 8 else None


class _WordHandler(SkippingHandler):
    skipped = frozenset({"del", "moveFrom", "footnoteReference", "endnoteReference"})

    def __init__(self, style_levels: dict[str, int], label: str) -> None:
        super().__init__()
        self.style_levels = style_levels
        self.label = label
        self.blocks: list[Block] = []
        self.budget = TextBudget()
        self.root_checked = False
        self.depth = 0
        self.block_kind: str | None = None
        self.block_depth = 0
        self.parts: list[str] = []
        self.heading: int | None = None
        self.in_text = False
        self.table_depth = 0
        self.rows: list[str] = []
        self.cells: list[str] = []
        self.cell_paragraphs: list[str] = []

    def on_start(self, name: str, attrs: dict[str, str]) -> None:
        self.depth += 1
        if not self.root_checked:
            self.root_checked = True
            if name != "document":
                raise MalformedDocumentError("document is not a Word document")
        if self.block_kind is None:
            if name == "p":
                self.block_kind, self.block_depth = "paragraph", self.depth
                self.parts, self.heading = [], None
            elif name == "tbl":
                self.block_kind, self.block_depth = "table", self.depth
                self.table_depth, self.rows = 1, []
            return
        if name == "t":
            self.in_text = True
        elif name == "tab":
            self.parts.append("\t")
        elif name in ("br", "cr"):
            self.parts.append("\n")
        elif name == "noBreakHyphen":
            self.parts.append("-")
        elif self.block_kind == "paragraph" and self.depth == self.block_depth + 2:
            # Paragraph properties sit at w:p/w:pPr/<property>.
            if name == "pStyle":
                level = self.style_levels.get(attribute(attrs, "val") or "")
                if level is not None and self.heading is None:
                    self.heading = level
            elif name == "outlineLvl":
                level = _outline_level(attribute(attrs, "val"))
                self.heading = min(level, MAX_HEADING_LEVEL) if level else None
        elif self.block_kind == "table":
            if name == "tbl":
                self.table_depth += 1
            elif name == "tr" and self.table_depth == 1:
                self.cells = []
            elif name == "tc" and self.table_depth == 1:
                self.cell_paragraphs = []
            elif name == "p":
                self.parts = []

    def on_data(self, text: str) -> None:
        if self.in_text:
            self.parts.append(text)

    def on_end(self, name: str) -> None:
        depth = self.depth
        self.depth -= 1
        if name == "t":
            self.in_text = False
            return
        if self.block_kind is None:
            return
        if depth == self.block_depth:
            if self.block_kind == "paragraph":
                text = self.budget.add("".join(self.parts).strip())
                self.blocks.append(
                    Block(len(self.blocks) + 1, text, self.heading if text else None)
                )
            else:
                text = self.budget.add("\n".join(row for row in self.rows if row))
                self.blocks.append(Block(len(self.blocks) + 1, text, kind="table"))
            self.block_kind = None
            return
        if self.block_kind != "table":
            return
        if name == "p":
            paragraph = plain_title("".join(self.parts))
            if paragraph:
                self.cell_paragraphs.append(paragraph)
        elif name == "tbl":
            self.table_depth -= 1
        elif name == "tc" and self.table_depth == 1:
            self.cells.append(" ".join(self.cell_paragraphs))
        elif name == "tr" and self.table_depth == 1:
            cells = trim_cells(self.cells)
            if any(cells):
                self.rows.append(" | ".join(cells))


def parse_docx(raw: bytes) -> tuple[DocumentSection, ...]:
    package = open_package(raw, "Word document")
    part = main_part(package, "a Word document")
    styles = related_parts(part_relationships(package, part), "styles")
    handler = _WordHandler(
        _style_levels(package, styles[0] if styles else None), package.label
    )
    stream_xml(package.read(part), handler, label=package.label, budget=package.elements)
    return flow_sections(handler.blocks, "docx")


# ---------------------------------------------------------------------------
# Excel


def is_date_format(code: str) -> bool:
    cleaned = re.sub(r'"[^"]*"|\\.|\[[^\]]*\]|_.|\*.', "", code).casefold()
    if cleaned in ("", "general", "@"):
        return False
    return bool(re.search(r"[dmyhs]", cleaned))


def excel_serial_to_text(serial: float, date1904: bool = False) -> str:
    if date1904:
        base = dt.datetime(1904, 1, 1)
    elif serial >= 61:
        base = dt.datetime(1899, 12, 30)
    elif serial >= 60:
        # Excel's 1900 system keeps Lotus 1-2-3's nonexistent 29 February 1900.
        return "1900-02-29"
    else:
        base = dt.datetime(1899, 12, 31)
    if not -1e6 < serial < 3e6:
        return format_number(serial)
    moment = base + dt.timedelta(seconds=round(serial * 86400))
    if serial == int(serial):
        return moment.date().isoformat()
    if 0 <= serial < 1:
        return moment.time().isoformat()
    return moment.isoformat(sep=" ")


def format_number(value: float) -> str:
    # Excel shows at most 15 significant digits; this also drops float noise.
    text = f"{value:.15g}"
    return "0" if text == "-0" else text


class _StylesHandler(SkippingHandler):
    def __init__(self) -> None:
        super().__init__()
        self.formats: dict[int, str] = {}
        self.cell_formats: list[int] = []
        self.in_cell_xfs = False

    def on_start(self, name: str, attrs: dict[str, str]) -> None:
        if name == "numFmt":
            try:
                self.formats[int(attribute(attrs, "numFmtId") or "")] = (
                    attribute(attrs, "formatCode") or ""
                )
            except ValueError:
                pass
        elif name == "cellXfs":
            self.in_cell_xfs = True
        elif name == "xf" and self.in_cell_xfs:
            try:
                self.cell_formats.append(int(attribute(attrs, "numFmtId") or "0"))
            except ValueError:
                self.cell_formats.append(0)

    def on_end(self, name: str) -> None:
        if name == "cellXfs":
            self.in_cell_xfs = False


class _SharedStringsHandler(SkippingHandler):
    skipped = frozenset({"rPh", "phoneticPr"})

    def __init__(self) -> None:
        super().__init__()
        self.strings: list[str] = []
        self.parts: list[str] = []
        self.in_text = False

    def on_start(self, name: str, attrs: dict[str, str]) -> None:
        if name == "si":
            self.parts = []
        elif name == "t":
            self.in_text = True

    def on_data(self, text: str) -> None:
        if self.in_text:
            self.parts.append(text)

    def on_end(self, name: str) -> None:
        if name == "t":
            self.in_text = False
        elif name == "si":
            self.strings.append("".join(self.parts))


@dataclass(slots=True)
class _CellState:
    column: int
    kind: str
    style: int
    value: list[str] = field(default_factory=list)
    inline: list[str] = field(default_factory=list)


class _SheetHandler(SkippingHandler):
    skipped = frozenset({"rPh", "f", "extLst"})

    def __init__(
        self,
        shared: list[str],
        date_styles: frozenset[int],
        date1904: bool,
        budget: TextBudget,
        label: str,
    ) -> None:
        super().__init__()
        self.shared = shared
        self.date_styles = date_styles
        self.date1904 = date1904
        self.budget = budget
        self.label = label
        self.rows: list[Row] = []
        self.row_number = 0
        self.next_column = 1
        self.values: dict[int, str] = {}
        self.cell: _CellState | None = None
        self.target: list[str] | None = None

    def on_start(self, name: str, attrs: dict[str, str]) -> None:
        if name == "row":
            reference = attribute(attrs, "r")
            self.row_number = (
                int(reference) if reference and reference.isdigit() else self.row_number + 1
            )
            self.next_column, self.values = 1, {}
        elif name == "c":
            column = self.next_column
            match = _CELL_REFERENCE.match(attribute(attrs, "r") or "")
            if match:
                column = 0
                for letter in match.group(1).upper():
                    column = column * 26 + ord(letter) - 64
            if column > MAX_SPREADSHEET_COLUMNS:
                raise DocumentTooLargeError(
                    f"{self.label} uses more than {MAX_SPREADSHEET_COLUMNS} columns"
                )
            try:
                style = int(attribute(attrs, "s") or "0")
            except ValueError:
                style = 0
            self.cell = _CellState(column, attribute(attrs, "t") or "n", style)
            self.next_column = column + 1
        elif self.cell is not None and name == "v":
            self.target = self.cell.value
        elif self.cell is not None and name == "t":
            self.target = self.cell.inline

    def on_data(self, text: str) -> None:
        if self.target is not None:
            self.target.append(text)

    def on_end(self, name: str) -> None:
        if name in ("v", "t"):
            self.target = None
        elif name == "c" and self.cell is not None:
            text = self._cell_text(self.cell)
            if text:
                self.values[self.cell.column] = self.budget.add(text)
            self.cell = None
        elif name == "row":
            self._finish_row()

    def _cell_text(self, cell: _CellState) -> str:
        raw = "".join(cell.value)
        kind = cell.kind
        if kind == "inlineStr":
            return "".join(cell.inline)
        if kind == "s":
            try:
                return self.shared[int(raw)]
            except (ValueError, IndexError):
                return ""
        if kind == "b":
            return {"1": "TRUE", "0": "FALSE"}.get(raw.strip(), raw)
        if kind in ("str", "e", "d"):
            return raw
        if not raw.strip():
            return ""
        try:
            number = float(raw)
        except ValueError:
            return raw
        if cell.style in self.date_styles:
            return excel_serial_to_text(number, self.date1904)
        return format_number(number)

    def _finish_row(self) -> None:
        if not self.values:
            return
        width = max(self.values)
        if width <= MAX_PADDED_COLUMNS:
            cells = trim_cells(self.values.get(column, "") for column in range(1, width + 1))
        else:
            cells = tuple(
                f"{column_letters(column)}{self.row_number}={value}"
                for column, value in sorted(self.values.items())
            )
        self.rows.append(Row(self.row_number, cells))


class _WorkbookHandler(SkippingHandler):
    def __init__(self, label: str) -> None:
        super().__init__()
        self.label = label
        self.sheets: list[tuple[str, str]] = []
        self.date1904 = False
        self.root_checked = False

    def on_start(self, name: str, attrs: dict[str, str]) -> None:
        if not self.root_checked:
            self.root_checked = True
            if name != "workbook":
                raise MalformedDocumentError("document is not an Excel workbook")
        if name == "workbookPr":
            self.date1904 = (attribute(attrs, "date1904") or "").casefold() in ("1", "true")
        elif name == "sheet":
            identifier = relationship_id(attrs)
            if identifier:
                self.sheets.append((attribute(attrs, "name") or "", identifier))


def parse_xlsx(raw: bytes) -> tuple[DocumentSection, ...]:
    package = open_package(raw, "Excel workbook")
    part = main_part(package, "an Excel workbook")
    workbook = _WorkbookHandler(package.label)
    stream_xml(package.read(part), workbook, label=package.label, budget=package.elements)
    relationships = part_relationships(package, part)

    shared: list[str] = []
    for shared_part in related_parts(relationships, "sharedStrings")[:1]:
        strings = _SharedStringsHandler()
        stream_xml(package.read(shared_part), strings, label=package.label, budget=package.elements)
        shared = strings.strings

    date_styles: frozenset[int] = frozenset()
    for styles_part in related_parts(relationships, "styles")[:1]:
        styles = _StylesHandler()
        stream_xml(package.read(styles_part), styles, label=package.label, budget=package.elements)
        date_styles = frozenset(
            index
            for index, format_id in enumerate(styles.cell_formats)
            if format_id in _BUILTIN_DATE_FORMATS
            or (format_id in styles.formats and is_date_format(styles.formats[format_id]))
        )

    budget = TextBudget()
    sheets: list[Sheet] = []
    for index, (name, identifier) in enumerate(workbook.sheets, start=1):
        relationship = relationships.get(identifier)
        if (
            relationship is None
            or relationship.part is None
            or relationship.type.rsplit("/", 1)[-1] != "worksheet"
            or not package.has(relationship.part)
        ):
            continue
        handler = _SheetHandler(shared, date_styles, workbook.date1904, budget, package.label)
        stream_xml(package.read(relationship.part), handler, label=package.label, budget=package.elements)
        sheets.append(Sheet(name, index, tuple(handler.rows)))
    return workbook_sections(sheets, "xlsx")


# ---------------------------------------------------------------------------
# PowerPoint


@dataclass(slots=True)
class _Shape:
    placeholder: str | None = None
    paragraphs: list[str] = field(default_factory=list)


class _SlideHandler(SkippingHandler):
    """Collects DrawingML text per shape, in reading order, with table rows."""

    _SHAPES = frozenset({"sp", "graphicFrame", "cxnSp", "pic"})

    def __init__(self, budget: TextBudget) -> None:
        super().__init__()
        self.budget = budget
        self.shapes: list[_Shape] = []
        self.shape: _Shape | None = None
        self.parts: list[str] | None = None
        self.in_text = False
        self.table_depth = 0
        self.cells: list[str] = []
        self.cell_paragraphs: list[str] = []

    def on_start(self, name: str, attrs: dict[str, str]) -> None:
        if name in self._SHAPES and self.shape is None:
            self.shape = _Shape()
        elif self.shape is None:
            return
        elif name == "ph":
            self.shape.placeholder = attribute(attrs, "type") or "body"
        elif name == "p":
            self.parts = []
        elif name == "t":
            self.in_text = True
        elif name == "br" and self.parts is not None:
            self.parts.append("\n")
        elif name == "tbl":
            self.table_depth += 1
        elif name == "tr" and self.table_depth == 1:
            self.cells = []
        elif name == "tc" and self.table_depth == 1:
            self.cell_paragraphs = []

    def on_data(self, text: str) -> None:
        if self.in_text and self.parts is not None:
            self.parts.append(text)

    def on_end(self, name: str) -> None:
        if self.shape is None:
            return
        if name == "t":
            self.in_text = False
        elif name == "p" and self.parts is not None:
            paragraph = "".join(self.parts).strip()
            self.parts = None
            if paragraph:
                if self.table_depth:
                    self.cell_paragraphs.append(plain_title(paragraph))
                else:
                    self.shape.paragraphs.append(self.budget.add(paragraph))
        elif name == "tc" and self.table_depth == 1:
            self.cells.append(" ".join(self.cell_paragraphs))
        elif name == "tr" and self.table_depth == 1:
            cells = trim_cells(self.cells)
            if any(cells):
                self.shape.paragraphs.append(self.budget.add(" | ".join(cells)))
        elif name == "tbl":
            self.table_depth -= 1
        elif name in self._SHAPES:
            self.shapes.append(self.shape)
            self.shape = None


class _PresentationHandler(SkippingHandler):
    def __init__(self, label: str) -> None:
        super().__init__()
        self.label = label
        self.slide_ids: list[str] = []
        self.root_checked = False

    def on_start(self, name: str, attrs: dict[str, str]) -> None:
        if not self.root_checked:
            self.root_checked = True
            if name != "presentation":
                raise MalformedDocumentError("document is not a PowerPoint deck")
        if name == "sldId":
            identifier = relationship_id(attrs)
            if identifier:
                self.slide_ids.append(identifier)


def _slide_text(
    package: SafeZip, part: str, budget: TextBudget
) -> list[_Shape]:
    handler = _SlideHandler(budget)
    stream_xml(package.read(part), handler, label=package.label, budget=package.elements)
    return handler.shapes


def parse_pptx(raw: bytes) -> tuple[DocumentSection, ...]:
    package = open_package(raw, "PowerPoint deck")
    part = main_part(package, "a PowerPoint deck")
    presentation = _PresentationHandler(package.label)
    stream_xml(package.read(part), presentation, label=package.label, budget=package.elements)
    relationships = part_relationships(package, part)
    budget = TextBudget()
    slides: list[Slide] = []
    for number, identifier in enumerate(presentation.slide_ids, start=1):
        relationship = relationships.get(identifier)
        if relationship is None or relationship.part is None:
            continue
        if not package.has(relationship.part):
            continue
        shapes = _slide_text(package, relationship.part, budget)
        title = next(
            (
                " ".join(shape.paragraphs)
                for shape in shapes
                if shape.placeholder in ("title", "ctrTitle") and shape.paragraphs
            ),
            "",
        )
        body = "\n".join(
            paragraph for shape in shapes for paragraph in shape.paragraphs
        )
        notes = ""
        slide_relationships = part_relationships(package, relationship.part)
        for notes_part in related_parts(slide_relationships, "notesSlide")[:1]:
            if package.has(notes_part):
                notes = "\n".join(
                    paragraph
                    for shape in _slide_text(package, notes_part, budget)
                    if shape.placeholder == "body"
                    for paragraph in shape.paragraphs
                )
        slides.append(Slide(number, title, body, notes))
    return slide_sections(slides, "pptx")
