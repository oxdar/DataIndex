"""Readers for legacy binary Office files: Word 97-2003 (.doc), Excel 97-2003
(.xls) and PowerPoint 97-2003 (.ppt).

``.xls`` uses the optional ``xlrd`` package. ``.doc`` and ``.ppt`` text is read
from their OLE2 streams with the optional ``olefile`` package, following the
published [MS-DOC] and [MS-PPT] structures. Only the text needed for retrieval is
decoded; formatting, embedded objects and macros are ignored and never run.
"""

from __future__ import annotations

import bisect
import io
import struct
from collections.abc import Iterator
from dataclasses import dataclass

from dataindex_ext_documents.exceptions import (
    DocumentIndexError,
    DocumentReaderUnavailableError,
    DocumentTooLargeError,
    EmptyDocumentError,
    MalformedDocumentError,
    ProtectedDocumentError,
)
from dataindex_ext_documents.formats._base import (
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
from dataindex_ext_documents.formats.office import excel_serial_to_text, format_number

_INSTALL_HINT = 'install it with: pip install "dataindex-ext-documents[legacy-office]"'
MAX_HEADING_LEVEL = 6
MAX_RECORDS = 2_000_000
MAX_RECORD_DEPTH = 64
MAX_DOC_CHARACTERS = 20_000_000


def _import_olefile():  # type: ignore[no-untyped-def]
    try:
        import olefile
    except ImportError as error:
        raise DocumentReaderUnavailableError(
            f"this format needs the optional olefile package; {_INSTALL_HINT}"
        ) from error
    return olefile


class _Ole:
    def __init__(self, raw: bytes, label: str) -> None:
        olefile = _import_olefile()
        self.label = label
        if not olefile.isOleFile(data=raw):
            raise MalformedDocumentError(f"document is not a {label} (OLE2) file")
        try:
            self._ole = olefile.OleFileIO(io.BytesIO(raw))
        except Exception as error:  # noqa: BLE001 - olefile raises several types
            raise MalformedDocumentError(f"{label} container is damaged") from error

    def has(self, name: str) -> bool:
        return bool(self._ole.exists(name))

    def read(self, name: str) -> bytes:
        try:
            return self._ole.openstream(name).read()
        except Exception as error:  # noqa: BLE001
            raise MalformedDocumentError(f"{self.label} stream {name!r} is damaged") from error

    def close(self) -> None:
        self._ole.close()


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _guard(label: str, function, *args):  # type: ignore[no-untyped-def]
    try:
        return function(*args)
    except DocumentIndexError:
        raise
    except Exception as error:  # noqa: BLE001 - corrupt binary structures
        raise MalformedDocumentError(f"{label} is damaged: {type(error).__name__}") from error


# ---------------------------------------------------------------------------
# Excel 97-2003


def parse_xls(raw: bytes) -> tuple[DocumentSection, ...]:
    try:
        import xlrd
    except ImportError as error:
        raise DocumentReaderUnavailableError(
            f".xls support needs the optional xlrd package; {_INSTALL_HINT}"
        ) from error
    try:
        book = xlrd.open_workbook(
            file_contents=raw, logfile=io.StringIO(), on_demand=True
        )
    except xlrd.XLRDError as error:
        if "encrypt" in str(error).casefold():
            raise ProtectedDocumentError(
                "workbook is password protected; remove the password and submit it again"
            ) from error
        raise MalformedDocumentError(f"document is not a readable .xls workbook: {error}") from error
    except Exception as error:  # noqa: BLE001 - xlrd raises many corruption types
        raise MalformedDocumentError("document is not a readable .xls workbook") from error
    try:
        return workbook_sections(_guard("workbook", _xls_sheets, book, xlrd), "xls")
    finally:
        book.release_resources()


def _xls_sheets(book, xlrd) -> list[Sheet]:  # type: ignore[no-untyped-def]
    budget = TextBudget()
    sheets: list[Sheet] = []
    date1904 = book.datemode == 1
    for index in range(book.nsheets):
        sheet = book.sheet_by_index(index)
        rows: list[Row] = []
        for row_index in range(sheet.nrows):
            values: list[str] = []
            for cell in sheet.row(row_index):
                kind, value = cell.ctype, cell.value
                if kind == xlrd.XL_CELL_TEXT:
                    text = str(value)
                elif kind == xlrd.XL_CELL_NUMBER:
                    text = format_number(float(value))
                elif kind == xlrd.XL_CELL_DATE:
                    text = excel_serial_to_text(float(value), date1904)
                elif kind == xlrd.XL_CELL_BOOLEAN:
                    text = "TRUE" if value else "FALSE"
                elif kind == xlrd.XL_CELL_ERROR:
                    text = xlrd.error_text_from_code.get(value, "#ERROR")
                else:
                    text = ""
                values.append(budget.add(text))
            cells = trim_cells(values)
            if any(cells):
                rows.append(Row(row_index + 1, cells))
        sheets.append(Sheet(sheet.name, index + 1, tuple(rows)))
        book.unload_sheet(index)
    return sheets


# ---------------------------------------------------------------------------
# Word 97-2003


@dataclass(frozen=True, slots=True)
class _Piece:
    cp_start: int
    cp_end: int
    fc: int
    compressed: bool


@dataclass(frozen=True, slots=True)
class _Fib:
    table_stream: str
    ccp_text: int
    fc_clx: int
    lcb_clx: int
    fc_stshf: int
    lcb_stshf: int
    fc_bte_papx: int
    lcb_bte_papx: int


def _read_fib(word: bytes) -> _Fib:
    if len(word) < 0x22 or _u16(word, 0) != 0xA5EC:
        raise MalformedDocumentError("document is not a Word 97-2003 file")
    flags = _u16(word, 0x0A)
    if flags & 0x0100:
        raise ProtectedDocumentError(
            "document is password protected; remove the password and submit it again"
        )
    position = 0x20
    csw = _u16(word, position)
    position += 2 + csw * 2
    cslw = _u16(word, position)
    lw_start = position + 2
    ccp_text = _u32(word, lw_start + 3 * 4)
    position = lw_start + cslw * 4
    cb_rg_fc_lcb = _u16(word, position)
    blob = position + 2

    def pair(index: int) -> tuple[int, int]:
        if index >= cb_rg_fc_lcb:
            return 0, 0
        return _u32(word, blob + index * 8), _u32(word, blob + index * 8 + 4)

    fc_clx, lcb_clx = pair(33)
    fc_stshf, lcb_stshf = pair(1)
    fc_bte, lcb_bte = pair(13)
    return _Fib(
        "1Table" if flags & 0x0200 else "0Table",
        ccp_text,
        fc_clx,
        lcb_clx,
        fc_stshf,
        lcb_stshf,
        fc_bte,
        lcb_bte,
    )


def _pieces(table: bytes, fib: _Fib) -> list[_Piece]:
    clx = table[fib.fc_clx : fib.fc_clx + fib.lcb_clx]
    if len(clx) != fib.lcb_clx or not clx:
        raise MalformedDocumentError("Word document has no readable piece table")
    position = 0
    while position < len(clx) and clx[position] == 0x01:
        position += 3 + struct.unpack_from("<h", clx, position + 1)[0]
    if position >= len(clx) or clx[position] != 0x02:
        raise MalformedDocumentError("Word document has no readable piece table")
    length = _u32(clx, position + 1)
    plc = clx[position + 5 : position + 5 + length]
    count = (len(plc) - 4) // 12
    if count <= 0:
        raise MalformedDocumentError("Word document has an empty piece table")
    cps = struct.unpack_from(f"<{count + 1}I", plc, 0)
    pieces: list[_Piece] = []
    for index in range(count):
        fc_raw = _u32(plc, (count + 1) * 4 + index * 8 + 2)
        compressed = bool(fc_raw & 0x40000000)
        fc = fc_raw & 0x3FFFFFFF
        pieces.append(
            _Piece(cps[index], cps[index + 1], fc // 2 if compressed else fc, compressed)
        )
    return pieces


def _main_text(word: bytes, pieces: list[_Piece], ccp_text: int) -> str:
    if ccp_text > MAX_DOC_CHARACTERS:
        raise DocumentTooLargeError("Word document text exceeds the extraction limit")
    parts: list[str] = []
    total = 0
    for piece in sorted(pieces, key=lambda item: item.cp_start):
        if total >= ccp_text:
            break
        count = min(max(0, piece.cp_end - piece.cp_start), ccp_text - total)
        if piece.compressed:
            text = word[piece.fc : piece.fc + count].decode("cp1252", "replace")
        else:
            text = word[piece.fc : piece.fc + 2 * count].decode("utf-16-le", "replace")
        parts.append(text)
        total += count
    return "".join(parts)[:ccp_text]


@dataclass(frozen=True, slots=True)
class _ParagraphProperties:
    style: int | None = None
    outline: int | None = None
    in_table: bool = False
    row_end: bool = False


def _style_levels(table: bytes, fib: _Fib) -> dict[int, int]:
    """Heading levels by style index, from built-in style identifiers (sti 1-9)."""
    stsh = table[fib.fc_stshf : fib.fc_stshf + fib.lcb_stshf]
    if len(stsh) < 6:
        return {}
    cb_stshi = _u16(stsh, 0)
    cstd = _u16(stsh, 2)
    position = 2 + cb_stshi
    levels: dict[int, int] = {}
    for istd in range(min(cstd, 4096)):
        if position + 2 > len(stsh):
            break
        cb_std = _u16(stsh, position)
        if cb_std >= 2:
            sti = _u16(stsh, position + 2) & 0x0FFF
            if 1 <= sti <= 9:
                levels[istd] = min(sti, MAX_HEADING_LEVEL)
            elif sti == 62:  # Title
                levels[istd] = 1
        position += 2 + cb_std
    return levels


_SPRM_SIZES = {0: 1, 1: 1, 2: 2, 3: 4, 4: 2, 5: 2, 7: 3}
_SPRM_OUTLINE = 0x2640
_SPRM_IN_TABLE = 0x2416
_SPRM_ROW_END = 0x2417


def _paragraph_sprms(istd: int, grpprl: bytes) -> _ParagraphProperties:
    outline: int | None = None
    in_table = row_end = False
    position = 0
    while position + 2 <= len(grpprl):
        sprm = _u16(grpprl, position)
        position += 2
        spra = sprm >> 13
        if spra == 6:
            if position >= len(grpprl):
                break
            size = grpprl[position] + 1
        else:
            size = _SPRM_SIZES[spra]
        if position < len(grpprl):
            operand = grpprl[position]
            if sprm == _SPRM_OUTLINE:
                # 0 means "no explicit level"; 9 is body text.
                outline = min(operand + 1, MAX_HEADING_LEVEL) if operand <= 8 else 0
            elif sprm == _SPRM_IN_TABLE:
                in_table = bool(operand)
            elif sprm == _SPRM_ROW_END:
                row_end = bool(operand)
        position += size
    return _ParagraphProperties(istd, outline, in_table, row_end)


def _paragraph_runs(
    word: bytes, table: bytes, fib: _Fib
) -> list[tuple[int, int, _ParagraphProperties]]:
    """(fc_start, fc_end, properties) for each paragraph run in the PAPX FKPs."""
    plc = table[fib.fc_bte_papx : fib.fc_bte_papx + fib.lcb_bte_papx]
    count = (len(plc) - 4) // 8
    runs: list[tuple[int, int, _ParagraphProperties]] = []
    for index in range(max(0, count)):
        page = _u32(plc, (count + 1) * 4 + index * 4) & 0x3FFFFF
        fkp = word[page * 512 : page * 512 + 512]
        if len(fkp) < 512:
            continue
        crun = fkp[511]
        fcs = struct.unpack_from(f"<{crun + 1}I", fkp, 0)
        for run in range(crun):
            offset = fkp[(crun + 1) * 4 + run * 13] * 2
            properties = _ParagraphProperties()
            if offset:
                cb = fkp[offset]
                start = offset + 1
                if cb == 0:
                    cb, start = fkp[offset + 1], offset + 2
                    size = 2 * cb
                else:
                    size = 2 * cb - 1
                grpprl = fkp[start : start + size]
                if len(grpprl) >= 2:
                    properties = _paragraph_sprms(_u16(grpprl, 0), grpprl[2:])
            runs.append((fcs[run], fcs[run + 1], properties))
    return sorted(runs, key=lambda item: item[0])


class _ParagraphLookup:
    """Paragraph properties by character position; empty when unreadable."""

    def __init__(self, word: bytes, table: bytes, fib: _Fib, pieces: list[_Piece]) -> None:
        self.pieces = sorted(pieces, key=lambda piece: piece.cp_start)
        self.piece_starts = [piece.cp_start for piece in self.pieces]
        try:
            self.styles = _style_levels(table, fib)
            self.runs = _paragraph_runs(word, table, fib)
        except (struct.error, IndexError, KeyError):
            self.styles, self.runs = {}, []
        self.run_starts = [run[0] for run in self.runs]

    @property
    def available(self) -> bool:
        return bool(self.runs)

    def at(self, cp: int) -> _ParagraphProperties | None:
        index = bisect.bisect_right(self.piece_starts, cp) - 1
        if index < 0 or not self.pieces[index].cp_start <= cp < self.pieces[index].cp_end:
            return None
        piece = self.pieces[index]
        fc = piece.fc + (cp - piece.cp_start) * (1 if piece.compressed else 2)
        run = bisect.bisect_right(self.run_starts, fc) - 1
        if run < 0 or not self.runs[run][0] <= fc < self.runs[run][1]:
            return None
        return self.runs[run][2]

    def level(self, properties: _ParagraphProperties | None) -> int | None:
        if properties is None:
            return None
        if properties.outline is not None:
            return properties.outline or None
        return self.styles.get(properties.style) if properties.style is not None else None


_DROPPED = frozenset(
    chr(code)
    for code in range(0x20)
    if code not in (0x07, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x13, 0x14, 0x15, 0x1E)
)


class _WordText:
    """Turns the main document text into paragraph and table blocks.

    With readable paragraph properties, table cells and row ends come from the
    in-table and row-end flags. Without them, two consecutive cell marks are read
    as a row end, which misreads rows that end in an empty cell.
    """

    def __init__(self, lookup: _ParagraphLookup) -> None:
        self.lookup = lookup
        self.budget = TextBudget()
        self.blocks: list[Block] = []
        self.parts: list[str] = []
        self.cells: list[str] = []
        self.rows: list[str] = []
        self.after_cell = False

    def _flush_table(self) -> None:
        if self.cells:
            self._end_row()
        if self.rows:
            text = self.budget.add("\n".join(self.rows))
            self.blocks.append(Block(len(self.blocks) + 1, text, kind="table"))
            self.rows = []

    def _end_row(self) -> None:
        cells = trim_cells(self.cells)
        self.cells = []
        if any(cells):
            self.rows.append(" | ".join(cells))

    def paragraph(self, cp: int) -> None:
        properties = self.lookup.at(cp)
        in_table = properties.in_table if properties is not None else bool(self.cells)
        if in_table:
            # A paragraph mark inside a table cell separates lines in that cell.
            self.parts.append(" ")
            return
        text = "".join(self.parts).strip()
        self.parts = []
        self._flush_table()
        self.budget.add(text)
        level = self.lookup.level(properties) if text else None
        self.blocks.append(Block(len(self.blocks) + 1, text, level))

    def cell(self, cp: int) -> None:
        text = plain_title("".join(self.parts))
        self.parts = []
        properties = self.lookup.at(cp)
        if properties is not None and self.lookup.available:
            row_end = properties.row_end
        else:
            row_end = not text and self.after_cell
        if row_end:
            self._end_row()
            self.after_cell = False
            return
        self.cells.append(text)
        self.after_cell = True

    def feed(self, text: str) -> tuple[Block, ...]:
        fields: list[bool] = []  # per open field: True once past its separator
        for cp, char in enumerate(text):
            if char == "\x13":
                fields.append(False)
                continue
            if char == "\x14":
                if fields:
                    fields[-1] = True
                continue
            if char == "\x15":
                if fields:
                    fields.pop()
                continue
            if fields and not all(fields):
                continue
            if char == "\r" or (char == "\x0c" and "".join(self.parts).strip()):
                # A page break alone is stored as "\x0c\r"; only a section
                # break that directly follows text ends a paragraph itself.
                self.after_cell = False
                self.paragraph(cp)
            elif char == "\x0c":
                self.after_cell = False
            elif char == "\x07":
                self.cell(cp)
            else:
                self.after_cell = False
                if char == "\x0b":
                    self.parts.append("\n")
                elif char == "\x1e":
                    self.parts.append("-")
                elif char not in _DROPPED:
                    self.parts.append(char)
        tail = "".join(self.parts).strip()
        self.parts = []
        if self.cells and tail:
            self.cells.append(plain_title(tail))
            tail = ""
        self._flush_table()
        if tail:
            self.budget.add(tail)
            self.blocks.append(Block(len(self.blocks) + 1, tail))
        return tuple(self.blocks)


def _doc_blocks(ole: _Ole) -> tuple[Block, ...]:
    word = ole.read("WordDocument")
    fib = _read_fib(word)
    if not ole.has(fib.table_stream):
        raise MalformedDocumentError("Word document has no table stream")
    table = ole.read(fib.table_stream)
    pieces = _pieces(table, fib)
    text = _main_text(word, pieces, fib.ccp_text)
    return _WordText(_ParagraphLookup(word, table, fib, pieces)).feed(text)


def parse_doc(raw: bytes) -> tuple[DocumentSection, ...]:
    ole = _Ole(raw, "Word 97-2003 document")
    try:
        if ole.has("EncryptedPackage"):
            raise ProtectedDocumentError(
                "document is password protected; remove the password and submit it again"
            )
        if not ole.has("WordDocument"):
            raise MalformedDocumentError("document is not a Word 97-2003 file")
        blocks = _guard("Word document", _doc_blocks, ole)
    finally:
        ole.close()
    if not any(block.text.strip() for block in blocks):
        raise EmptyDocumentError("document contains no indexable text")
    return flow_sections(blocks, "doc")


# ---------------------------------------------------------------------------
# PowerPoint 97-2003

_DOCUMENT = 0x03E8
_SLIDE = 0x03EE
_NOTES = 0x03F0
_SLIDE_ATOM = 0x03EF
_SLIDE_LIST = 0x0FF0
_SLIDE_PERSIST = 0x03F3
_TEXT_HEADER = 0x0F9F
_TEXT_CHARS = 0x0FA0
_TEXT_BYTES = 0x0FA8
_USER_EDIT = 0x0FF5
_PERSIST_DIRECTORY = 0x1772
_TITLE_TYPES = frozenset({0, 6})
_NOTES_TYPE = 2


@dataclass(frozen=True, slots=True)
class _Record:
    instance: int
    type: int
    start: int
    end: int
    container: bool


def _records(data: bytes, start: int, end: int, counter: list[int]) -> Iterator[_Record]:
    position = start
    end = min(end, len(data))
    while position + 8 <= end:
        header, kind, length = struct.unpack_from("<HHI", data, position)
        body = position + 8
        if body + length > end:
            return
        counter[0] += 1
        if counter[0] > MAX_RECORDS:
            raise DocumentTooLargeError("presentation has too many records")
        yield _Record(header >> 4, kind, body, body + length, header & 0x0F == 0x0F)
        position = body + length


def _texts(data: bytes, record: _Record, counter: list[int], depth: int = 0) -> Iterator[tuple[int, str]]:
    """(text type, text) for every text atom under ``record``, in stream order."""
    if depth > MAX_RECORD_DEPTH:
        return
    text_type = 4
    for child in _records(data, record.start, record.end, counter):
        if child.container:
            yield from _texts(data, child, counter, depth + 1)
        elif child.type == _TEXT_HEADER and child.end - child.start >= 4:
            text_type = _u32(data, child.start)
        elif child.type == _TEXT_CHARS:
            yield text_type, data[child.start : child.end].decode("utf-16-le", "replace")
        elif child.type == _TEXT_BYTES:
            yield text_type, data[child.start : child.end].decode("latin-1")


def _clean_slide_text(text: str) -> str:
    return "\n".join(
        line.strip() for line in text.replace("\x0b", "\n").replace("\r", "\n").split("\n") if line.strip()
    )


def _persist_map(document: bytes, offset: int) -> tuple[dict[int, int], int]:
    persist: dict[int, int] = {}
    seen: set[int] = set()
    document_ref = 0
    while offset and offset not in seen and len(seen) < 1000:
        seen.add(offset)
        if offset + 28 > len(document) or _u16(document, offset + 2) != _USER_EDIT:
            raise MalformedDocumentError("presentation edit history is damaged")
        body = offset + 8
        if not document_ref:
            document_ref = _u32(document, body + 16)
        directory = _u32(document, body + 12)
        if directory + 8 <= len(document) and _u16(document, directory + 2) == _PERSIST_DIRECTORY:
            length = _u32(document, directory + 4)
            position, end = directory + 8, min(len(document), directory + 8 + length)
            while position + 4 <= end:
                entry = _u32(document, position)
                first, count = entry & 0xFFFFF, entry >> 20
                position += 4
                for index in range(count):
                    if position + 4 > end:
                        break
                    persist.setdefault(first + index, _u32(document, position))
                    position += 4
        offset = _u32(document, body + 8)
    return persist, document_ref


def _container_at(document: bytes, offset: int | None, kind: int) -> _Record | None:
    if offset is None or offset + 8 > len(document):
        return None
    header, record_type, length = struct.unpack_from("<HHI", document, offset)
    if record_type != kind or offset + 8 + length > len(document):
        return None
    return _Record(header >> 4, record_type, offset + 8, offset + 8 + length, True)


def _ppt_slides(ole: _Ole) -> list[Slide]:
    current = ole.read("Current User")
    if len(current) < 20:
        raise MalformedDocumentError("presentation has no current-user record")
    if _u32(current, 12) == 0xF3D1C4DF:
        raise ProtectedDocumentError(
            "presentation is password protected; remove the password and submit it again"
        )
    document = ole.read("PowerPoint Document")
    persist, document_ref = _persist_map(document, _u32(current, 16))
    counter = [0]
    root = _container_at(document, persist.get(document_ref), _DOCUMENT)
    if root is None:
        raise MalformedDocumentError("presentation has no document record")

    slide_lists: dict[int, _Record] = {}
    for child in _records(document, root.start, root.end, counter):
        if child.type == _SLIDE_LIST and child.instance not in slide_lists:
            slide_lists[child.instance] = child

    def persisted(slide_list: _Record | None) -> list[tuple[int, int, list[tuple[int, str]]]]:
        """(persist id, slide id, outline texts) for each slide in a slide list."""
        entries: list[tuple[int, int, list[tuple[int, str]]]] = []
        if slide_list is None:
            return entries
        text_type = 4
        for child in _records(document, slide_list.start, slide_list.end, counter):
            if child.type == _SLIDE_PERSIST and child.end - child.start >= 16:
                entries.append((_u32(document, child.start), _u32(document, child.start + 12), []))
            elif not entries:
                continue
            elif child.type == _TEXT_HEADER and child.end - child.start >= 4:
                text_type = _u32(document, child.start)
            elif child.type == _TEXT_CHARS:
                entries[-1][2].append((text_type, document[child.start : child.end].decode("utf-16-le", "replace")))
            elif child.type == _TEXT_BYTES:
                entries[-1][2].append((text_type, document[child.start : child.end].decode("latin-1")))
        return entries

    notes_by_id = {slide_id: ref for ref, slide_id, _ in persisted(slide_lists.get(2))}
    budget = TextBudget()
    slides: list[Slide] = []
    for number, (ref, _slide_id, outline_texts) in enumerate(persisted(slide_lists.get(0)), start=1):
        texts = list(outline_texts)
        container = _container_at(document, persist.get(ref), _SLIDE)
        notes_ref: int | None = None
        if container is not None:
            texts.extend(_texts(document, container, counter))
            for child in _records(document, container.start, container.end, counter):
                if child.type == _SLIDE_ATOM and child.end - child.start >= 20:
                    notes_ref = notes_by_id.get(_u32(document, child.start + 16))
                    break
        seen: set[str] = set()
        title = ""
        lines: list[str] = []
        for text_type, raw_text in texts:
            text = _clean_slide_text(raw_text)
            if not text or text in seen or text_type == _NOTES_TYPE:
                continue
            seen.add(text)
            if text_type in _TITLE_TYPES and not title:
                title = plain_title(text)
            lines.append(budget.add(text))
        notes = ""
        notes_container = _container_at(document, persist.get(notes_ref) if notes_ref else None, _NOTES)
        if notes_container is not None:
            notes = "\n".join(
                dict.fromkeys(
                    budget.add(_clean_slide_text(text))
                    for text_type, text in _texts(document, notes_container, counter)
                    if text_type == _NOTES_TYPE and _clean_slide_text(text)
                )
            )
        slides.append(Slide(number, title, "\n".join(lines), notes))
    return slides


def parse_ppt(raw: bytes) -> tuple[DocumentSection, ...]:
    ole = _Ole(raw, "PowerPoint 97-2003 presentation")
    try:
        if ole.has("EncryptedPackage"):
            raise ProtectedDocumentError(
                "presentation is password protected; remove the password and submit it again"
            )
        if not (ole.has("PowerPoint Document") and ole.has("Current User")):
            raise MalformedDocumentError("document is not a PowerPoint 97-2003 file")
        slides = _guard("presentation", _ppt_slides, ole)
    finally:
        ole.close()
    sections = slide_sections(slides, "ppt")
    if not sections:
        raise EmptyDocumentError("presentation contains no indexable text")
    return sections
