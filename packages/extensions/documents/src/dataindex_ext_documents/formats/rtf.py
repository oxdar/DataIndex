"""Reader for Rich Text Format (.rtf).

A small tokenizer walks the group structure. Non-text destinations (fonts,
colours, metadata, pictures, headers, footers, footnotes, field instructions) are
skipped; field results are kept. Paragraph outline levels and heading styles
from the stylesheet become section headings.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, replace

from dataindex_ext_documents.exceptions import (
    DocumentTooLargeError,
    EmptyDocumentError,
    MalformedDocumentError,
)
from dataindex_ext_documents.formats._base import (
    Block,
    DocumentSection,
    TextBudget,
    flow_sections,
    plain_title,
    trim_cells,
)

MAX_GROUP_DEPTH = 2000
MAX_HEADING_LEVEL = 6
_TOKEN = re.compile(
    r"\\([a-zA-Z]{1,32})(-?\d{1,10})? ?"
    r"|\\'([0-9a-fA-F]{2})"
    r"|\\(.)"
    r"|([{}])"
    r"|([\r\n]+)"
    r"|([^\\{}\r\n]+)",
    re.S,
)
_SKIPPED_DESTINATIONS = frozenset(
    {
        "annotation", "atnauthor", "atnid", "author", "buptim", "colortbl",
        "comment", "creatim", "datastore", "docvar", "doccomm", "filetbl",
        "fldinst", "footer", "footerf", "footerl", "footerr", "footnote",
        "ftncn", "ftnsep", "ftnsepc", "header", "headerf", "headerl",
        "headerr", "info", "keywords", "listoverridetable", "listtable",
        "listtext", "nonshppict", "object", "objdata", "operator", "pict",
        "pntext", "pntxta", "pntxtb", "printim", "private", "revtim",
        "revtbl", "rsidtbl", "sp", "subject", "template", "themedata",
        "title", "tc", "txe", "upr", "userprops", "xe", "xmlnstbl",
        "colorschememapping", "latentstyles", "generator", "mmathPr",
    }
)
_SYMBOLS = {
    "emdash": "\u2014", "endash": "\u2013", "bullet": "\u2022",
    "lquote": "\u2018", "rquote": "\u2019", "ldblquote": "\u201c",
    "rdblquote": "\u201d", "emspace": " ", "enspace": " ", "qmspace": " ",
    "tab": "\t", "line": "\n", "page": "\n", "column": "\n",
}
_CONTROL_SYMBOLS = {"~": "\u00a0", "_": "\u2011", "-": "", "\\": "\\", "{": "{", "}": "}"}
_CHARSET_CODEPAGES = {
    77: "mac_roman", 128: "cp932", 129: "cp949", 130: "johab", 134: "gbk",
    136: "cp950", 161: "cp1253", 162: "cp1254", 163: "cp1258", 177: "cp1255",
    178: "cp1256", 186: "cp1257", 204: "cp1251", 222: "cp874", 238: "cp1250",
    254: "cp437", 255: "cp850",
}


@dataclass(frozen=True, slots=True)
class _GroupState:
    skip: bool = False
    destination: str | None = None
    unicode_skip: int = 1
    font: int | None = None
    defined: int | None = None


def _codec(name: str) -> str | None:
    try:
        return codecs.lookup(name).name
    except LookupError:
        return None


class _RtfReader:
    def __init__(self, source: str) -> None:
        self.source = source
        self.codepage = "cp1252"
        self.default_font: int | None = None
        self.font_codepages: dict[int, str] = {}
        self.style_names: dict[int, list[str]] = {}
        self.style_outline: dict[int, int] = {}
        self.style_base: dict[int, int] = {}
        self.blocks: list[Block] = []
        self.budget = TextBudget()
        self.parts: list[str] = []
        self.pending = bytearray()
        self.pending_codec = "cp1252"
        self.skip_chars = 0
        # Paragraph properties persist until \pard, as in RTF itself.
        self.outline: int | None = None
        self.style = 0
        self.in_table = False
        self.row_cells: list[str] = []
        self.table_rows: list[str] = []

    # -- text output --------------------------------------------------------

    def _font_codec(self, state: _GroupState) -> str:
        font = state.font if state.font is not None else self.default_font
        return self.font_codepages.get(font, self.codepage) if font is not None else self.codepage

    def _flush(self) -> None:
        if self.pending:
            self.parts.append(bytes(self.pending).decode(self.pending_codec, "replace"))
            self.pending.clear()

    def _bytes(self, value: bytes, state: _GroupState) -> None:
        codec = self._font_codec(state)
        if codec != self.pending_codec:
            self._flush()
            self.pending_codec = codec
        self.pending.extend(value)

    def _text(self, value: str) -> None:
        self._flush()
        self.parts.append(value)

    def _take_parts(self) -> str:
        self._flush()
        text = "".join(self.parts)
        self.parts = []
        # \u escapes encode astral characters as UTF-16 surrogate pairs.
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")

    # -- structure ----------------------------------------------------------

    def _heading_level(self) -> int | None:
        if self.outline is not None:
            return self.outline
        seen: set[int] = set()
        style = self.style
        while style not in seen:
            seen.add(style)
            name = " ".join("".join(self.style_names.get(style, [])).split(";")[0].split())
            heading = re.fullmatch(r"heading\s*([1-9])", name.casefold())
            if heading:
                return min(int(heading.group(1)), MAX_HEADING_LEVEL)
            if name.casefold() == "title":
                return 1
            if style in self.style_outline:
                return self.style_outline[style]
            if style not in self.style_base:
                return None
            style = self.style_base[style]
        return None

    def _flush_table(self) -> None:
        if self.row_cells:
            self._end_row()
        if self.table_rows:
            text = self.budget.add("\n".join(self.table_rows))
            self.blocks.append(Block(len(self.blocks) + 1, text, kind="table"))
            self.table_rows = []

    def _end_paragraph(self) -> None:
        if self.in_table:
            self._text(" ")
            return
        text = self._take_parts().strip()
        self._flush_table()
        self.budget.add(text)
        self.blocks.append(
            Block(len(self.blocks) + 1, text, self._heading_level() if text else None)
        )

    def _end_cell(self) -> None:
        self.row_cells.append(plain_title(self._take_parts()))

    def _end_row(self) -> None:
        tail = plain_title(self._take_parts())
        if tail:
            self.row_cells.append(tail)
        cells = trim_cells(self.row_cells)
        self.row_cells = []
        if any(cells):
            self.table_rows.append(self.budget.add(" | ".join(cells)))

    # -- tokenizer ----------------------------------------------------------

    def run(self) -> tuple[Block, ...]:
        source = self.source
        state = _GroupState()
        stack: list[_GroupState] = []
        position = 0
        length = len(source)
        while position < length:
            match = _TOKEN.match(source, position)
            if match is None:
                # A lone trailing backslash.
                break
            position = match.end()
            word, parameter, hex_byte, symbol, brace, _newline, text = match.groups()
            if brace == "{":
                if len(stack) >= MAX_GROUP_DEPTH:
                    raise DocumentTooLargeError(
                        f"RTF nests groups deeper than {MAX_GROUP_DEPTH} levels"
                    )
                stack.append(state)
                self.skip_chars = 0
                if (
                    state.destination == "stylesheet"
                    and state.defined is None
                    and not state.skip
                ):
                    # Each child group of the stylesheet defines one style (0 unless \sN).
                    state = replace(state, defined=0)
                continue
            if brace == "}":
                self.skip_chars = 0
                if not stack:
                    break
                state = stack.pop()
                continue
            if word is not None:
                if word == "bin" and parameter:
                    # Raw binary data follows; it is never text.
                    position += max(0, int(parameter))
                    continue
                state = self._control_word(word, parameter, state)
                continue
            if state.skip:
                continue
            if hex_byte is not None:
                if self.skip_chars:
                    self.skip_chars -= 1
                    continue
                self._route_bytes(bytes([int(hex_byte, 16)]), state)
            elif symbol is not None:
                if symbol == "*":
                    state = replace(state, skip=True)
                elif symbol in ("\n", "\r"):
                    if state.destination is None:
                        self._end_paragraph()
                elif self.skip_chars:
                    self.skip_chars -= 1
                elif symbol in _CONTROL_SYMBOLS:
                    self._route_text(_CONTROL_SYMBOLS[symbol], state)
            elif text is not None:
                if self.skip_chars:
                    dropped = min(self.skip_chars, len(text))
                    self.skip_chars -= dropped
                    text = text[dropped:]
                if text:
                    self._route_bytes(text.encode("latin-1"), state)
        self._flush_table()
        tail = self._take_parts().strip()
        if tail:
            self.budget.add(tail)
            self.blocks.append(Block(len(self.blocks) + 1, tail, self._heading_level()))
        return tuple(self.blocks)

    def _route_bytes(self, value: bytes, state: _GroupState) -> None:
        if state.destination is None:
            self._bytes(value, state)
        elif state.destination == "stylesheet" and state.defined is not None:
            names = self.style_names.setdefault(state.defined, [])
            if sum(len(part) for part in names) < 512:
                names.append(value.decode(self._font_codec(state), "replace"))

    def _route_text(self, value: str, state: _GroupState) -> None:
        if state.destination is None:
            self._text(value)

    def _control_word(
        self, word: str, parameter: str | None, state: _GroupState
    ) -> _GroupState:
        number = int(parameter) if parameter else None
        if state.skip:
            return state
        if word in _SKIPPED_DESTINATIONS:
            return replace(state, skip=True)
        if word in ("fonttbl", "stylesheet"):
            return replace(state, destination=word)
        if word == "u" and number is not None:
            if self.skip_chars:
                self.skip_chars = 0
            code = number + 65536 if number < 0 else number
            self._route_text(chr(code), state)
            self.skip_chars = state.unicode_skip
            return state
        if self.skip_chars:
            self.skip_chars -= 1
        if word == "uc" and number is not None:
            return replace(state, unicode_skip=max(0, min(number, 8)))
        if state.destination == "fonttbl":
            if word == "f" and number is not None:
                return replace(state, defined=number)
            if word == "fcharset" and number is not None and state.defined is not None:
                codec = _CHARSET_CODEPAGES.get(number)
                if codec and _codec(codec):
                    self.font_codepages[state.defined] = codec
            if word == "cpg" and number and state.defined is not None:
                codec = _codec(f"cp{number}")
                if codec:
                    self.font_codepages[state.defined] = codec
            return state
        if state.destination == "stylesheet":
            if state.defined is None:
                return state
            if word == "s" and number is not None:
                return replace(state, defined=number)
            if word == "outlinelevel" and number is not None and 0 <= number <= 8:
                self.style_outline[state.defined] = min(number + 1, MAX_HEADING_LEVEL)
            elif word == "sbasedon" and number is not None:
                self.style_base[state.defined] = number
            elif word in ("cs", "ds", "ts", "tsrowd"):
                return replace(state, skip=True)
            return state
        if state.destination is not None:
            return state
        return self._body_control(word, number, state)

    def _body_control(
        self, word: str, number: int | None, state: _GroupState
    ) -> _GroupState:
        if word in ("par", "sect"):
            self._end_paragraph()
        elif word == "pard":
            self.outline, self.style, self.in_table = None, 0, False
        elif word == "intbl":
            self.in_table = True
        elif word in ("cell", "nestcell"):
            self._end_cell()
        elif word in ("row", "nestrow"):
            self._end_row()
            self.in_table = False
        elif word == "outlinelevel" and number is not None:
            self.outline = min(number + 1, MAX_HEADING_LEVEL) if 0 <= number <= 8 else None
        elif word == "s" and number is not None:
            self.style = number
        elif word == "f" and number is not None:
            return replace(state, font=number)
        elif word == "deff" and number is not None:
            self.default_font = number
        elif word == "ansicpg" and number:
            self.codepage = _codec(f"cp{number}") or self.codepage
        elif word == "mac":
            self.codepage = "mac_roman"
        elif word == "pc":
            self.codepage = "cp437"
        elif word == "pca":
            self.codepage = "cp850"
        elif word in _SYMBOLS:
            self._text(_SYMBOLS[word])
        return state


def parse_rtf(raw: bytes) -> tuple[DocumentSection, ...]:
    source = raw.decode("latin-1")
    if not source.lstrip().startswith("{\\rtf"):
        raise MalformedDocumentError("document is not RTF: it must start with {\\rtf")
    blocks = _RtfReader(source.lstrip()).run()
    if not any(block.text.strip() for block in blocks):
        raise EmptyDocumentError("document contains no indexable text")
    return flow_sections(blocks, "rtf")
