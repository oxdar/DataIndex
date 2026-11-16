"""Readers for delimited tables, JSON, JSON Lines and generic XML."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from dataclasses import dataclass

from dataindex.models import NativeLocator
from dataindex_ext_documents.exceptions import (
    DocumentTooLargeError,
    EmptyDocumentError,
    MalformedDocumentError,
)
from dataindex_ext_documents.formats._base import (
    MAX_DOCUMENT_SECTIONS,
    MAX_GROUP_CHARS,
    MAX_GROUP_ITEMS,
    MAX_SPREADSHEET_COLUMNS,
    MAX_TITLE_CHARS,
    DocumentSection,
    Row,
    TextBudget,
    check_section_count,
    decode_text_with_bom,
    plain_title,
    source_lines,
    table_sections,
    too_many_sections,
)
from dataindex_ext_documents.formats._safexml import SafeXmlParser, decode_xml_document

MAX_NESTED_SECTION_DEPTH = 6
MAX_XML_DOCUMENT_ELEMENTS = 500_000
_NAME_KEYS = ("title", "name", "label", "id", "key")
# The header row plus as many full row groups as the section limit allows.
_MAX_TABLE_ROWS = 1 + MAX_DOCUMENT_SECTIONS * MAX_GROUP_ITEMS
# The sniffer's cost grows quickly with the number of sample lines.
_SNIFF_SAMPLE_CHARS = 8192


def _decode_nonempty(raw: bytes) -> str:
    text = decode_text_with_bom(raw)
    if not text.strip():
        raise EmptyDocumentError("document contains no indexable text")
    return text


def _size_groups(sizes: Sequence[int]) -> list[range]:
    """Consecutive index ranges of at most MAX_GROUP_ITEMS items or ~MAX_GROUP_CHARS."""
    groups: list[range] = []
    start, total = 0, 0
    for index, size in enumerate(sizes):
        if index > start and (
            index - start >= MAX_GROUP_ITEMS or total + size > MAX_GROUP_CHARS
        ):
            groups.append(range(start, index))
            start, total = index, 0
        total += size
    if start < len(sizes):
        groups.append(range(start, len(sizes)))
    return groups


# ---------------------------------------------------------------------------
# CSV and TSV


def parse_csv(raw: bytes) -> tuple[DocumentSection, ...]:
    return _parse_delimited(raw, None, "csv")


def parse_tsv(raw: bytes) -> tuple[DocumentSection, ...]:
    return _parse_delimited(raw, "\t", "tsv")


def _sniff_delimiter(text: str) -> str:
    sample = text[:_SNIFF_SAMPLE_CHARS]
    if len(text) > len(sample) and "\n" in sample:
        sample = sample[: sample.rindex("\n")]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def _parse_delimited(
    raw: bytes, delimiter: str | None, scheme: str
) -> tuple[DocumentSection, ...]:
    text = _decode_nonempty(raw)
    reader = csv.reader(
        io.StringIO(text, newline=""), delimiter=delimiter or _sniff_delimiter(text)
    )
    # The cells are a subset of the text, so one charge bounds them all.
    TextBudget().add(text)
    rows: list[Row] = []
    previous_line = 0
    try:
        for number, record in enumerate(reader, start=1):
            line_start, previous_line = previous_line + 1, reader.line_num
            while record and not record[-1].strip():
                record.pop()
            if not record:
                continue
            if len(record) > MAX_SPREADSHEET_COLUMNS:
                raise DocumentTooLargeError(
                    f"row {number} has more than {MAX_SPREADSHEET_COLUMNS} columns"
                )
            rows.append(Row(number, tuple(record), line_start, previous_line))
            if len(rows) > _MAX_TABLE_ROWS:
                raise too_many_sections()
    except csv.Error as error:
        raise MalformedDocumentError(
            f"delimited text could not be parsed near line {reader.line_num}: {error}"
        ) from error
    return table_sections(rows, scheme)


# ---------------------------------------------------------------------------
# JSON and JSON Lines


def _label(value: object) -> str:
    if isinstance(value, dict):
        for key in _NAME_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, (str, int, float)) and not isinstance(candidate, bool):
                return plain_title(str(candidate))[:80]
    return ""


def _describe(value: object) -> str:
    if isinstance(value, dict):
        return f"object with {len(value)} members"
    if isinstance(value, list):
        return f"array with {len(value)} items"
    return json.dumps(value, ensure_ascii=False)


def _pointer_token(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


class _JsonSections:
    def __init__(self) -> None:
        self.sections: list[DocumentSection] = []
        self.budget = TextBudget()

    def _add(self, section: DocumentSection) -> None:
        self.sections.append(section)
        check_section_count(len(self.sections))

    def _render(self, value: object) -> str:
        return self.budget.add(json.dumps(value, ensure_ascii=False))

    def container(
        self, value: dict | list, pointer: str, name: str, title: str, level: int
    ) -> None:
        if isinstance(value, list) and len(value) > MAX_GROUP_ITEMS:
            self._grouped_array(value, pointer, name, title, level)
            return
        if isinstance(value, dict):
            members = [
                (str(key), child, f"{pointer}/{_pointer_token(str(key))}")
                for key, child in value.items()
            ]
        else:
            members = [
                (f"{name}[{index}]", child, f"{pointer}/{index}")
                for index, child in enumerate(value)
            ]
        lines: list[str] = []
        children: list[tuple[dict | list, str, str]] = []
        for member_name, child, child_pointer in members:
            if (
                isinstance(child, (dict, list))
                and child
                and level < MAX_NESTED_SECTION_DEPTH
            ):
                lines.append(f"{member_name}: {_describe(child)}")
                children.append((child, child_pointer, member_name))
            else:
                lines.append(f"{member_name}: {self._render(child)}")
        text = "\n".join(lines) if lines else self._render(value)
        self._add(
            DocumentSection(
                stable_key=f"pointer:{pointer}",
                title=title,
                kind="json_object" if isinstance(value, dict) else "json_array",
                level=level,
                locator=NativeLocator("json-pointer", 1, {"pointer": pointer}),
                search_text=text,
                text=text,
            )
        )
        for child, child_pointer, member_name in children:
            label = _label(child)
            self.container(
                child,
                child_pointer,
                member_name,
                f"{member_name}: {label}" if label else member_name,
                level + 1,
            )

    def _grouped_array(
        self, value: list, pointer: str, name: str, title: str, level: int
    ) -> None:
        summary = f"{name or 'array'}: array with {len(value)} items"
        self._add(
            DocumentSection(
                stable_key=f"pointer:{pointer}",
                title=title,
                kind="json_array",
                level=level,
                locator=NativeLocator("json-pointer", 1, {"pointer": pointer}),
                search_text=summary,
                text=summary,
            )
        )
        rendered = [f"[{index}] {self._render(item)}" for index, item in enumerate(value)]
        for group in _size_groups([len(line) + 1 for line in rendered]):
            first, last = group.start, group.stop - 1
            labels = [label for label in (_label(value[first]), _label(value[last])) if label]
            span = f"{name}[{first}-{last}]" if name else f"Items {first}-{last}"
            text = "\n".join(rendered[first : last + 1])
            self._add(
                DocumentSection(
                    stable_key=f"items:{pointer}:{first}-{last}",
                    title=f"{span}: {' … '.join(labels)}" if labels else span,
                    kind="json_items",
                    level=level + 1,
                    locator=NativeLocator(
                        "json-items",
                        1,
                        {"pointer": pointer, "index_start": first, "index_end": last},
                    ),
                    search_text=text,
                    text=text,
                )
            )


def parse_json(raw: bytes) -> tuple[DocumentSection, ...]:
    text = _decode_nonempty(raw)
    try:
        value = json.loads(text)
    except RecursionError as error:
        raise DocumentTooLargeError("JSON nests too deeply to read") from error
    except ValueError as error:
        raise MalformedDocumentError(f"document is not valid JSON: {error}") from error
    builder = _JsonSections()
    try:
        if isinstance(value, (dict, list)):
            builder.container(value, "", "", _label(value) or "Document", 1)
        else:
            rendered = builder._render(value)
            builder._add(
                DocumentSection(
                    "pointer:",
                    "Document",
                    "json_value",
                    1,
                    NativeLocator("json-pointer", 1, {"pointer": ""}),
                    rendered,
                    rendered,
                )
            )
    except RecursionError as error:
        raise DocumentTooLargeError("JSON nests too deeply to read") from error
    return tuple(builder.sections)


def parse_jsonl(raw: bytes) -> tuple[DocumentSection, ...]:
    text = _decode_nonempty(raw)
    budget = TextBudget()
    records: list[tuple[int, str, str]] = []
    for number, line in enumerate(source_lines(text), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except RecursionError as error:
            raise DocumentTooLargeError(
                f"line {number} nests JSON too deeply to read"
            ) from error
        except ValueError as error:
            raise MalformedDocumentError(
                f"line {number} is not valid JSON: {error}"
            ) from error
        records.append((number, budget.add(line.rstrip("\r\n")), _label(value)))
    sections: list[DocumentSection] = []
    for group in _size_groups([len(record[1]) + 1 for record in records]):
        chosen = records[group.start : group.stop]
        first, last = chosen[0][0], chosen[-1][0]
        span = f"Line {first}" if first == last else f"Lines {first}-{last}"
        labels = list(dict.fromkeys(label for label in (chosen[0][2], chosen[-1][2]) if label))
        body = "\n".join(record[1] for record in chosen)
        sections.append(
            DocumentSection(
                stable_key=f"lines:{first}-{last}",
                title=f"{span}: {' … '.join(labels)}" if labels else span,
                kind="records",
                level=1,
                locator=NativeLocator(
                    "jsonl-lines", 1, {"line_start": first, "line_end": last}
                ),
                search_text=body,
                text=body,
            )
        )
        check_section_count(len(sections))
    return tuple(sections)


# ---------------------------------------------------------------------------
# XML


@dataclass(slots=True)
class _XmlNode:
    path: str
    title: str
    line_start: int
    line_end: int
    byte_start: int
    byte_end: int
    search_text: str
    children: list[_XmlNode]


class _XmlFrame:
    """An open element. Child bookkeeping is created when a first child starts."""

    __slots__ = (
        "name",
        "path",
        "attrs",
        "depth",
        "line_start",
        "byte_start",
        "own_text",
        "lines",
        "nodes",
        "counts",
        "hint",
    )

    def __init__(
        self,
        name: str,
        path: str,
        attrs: dict[str, str],
        depth: int,
        line_start: int,
        byte_start: int,
    ) -> None:
        self.name = name
        self.path = path
        self.attrs = attrs
        self.depth = depth
        self.line_start = line_start
        self.byte_start = byte_start
        self.own_text: list[str] = []
        self.lines: list[str] = []
        self.nodes: list[_XmlNode] = []
        self.counts: dict[str, int] | None = None
        self.hint = ""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class _XmlCollector:
    """Keeps only elements that contain elements, so memory follows the outline."""

    def __init__(self, source: bytes) -> None:
        self.source = source
        self.parser: SafeXmlParser | None = None
        self.stack: list[_XmlFrame] = []
        self.root: _XmlNode | None = None
        self.root_leaf: _XmlFrame | None = None
        self.root_leaf_text = ""
        self.budget = TextBudget()

    def _position(self) -> tuple[int, int]:
        assert self.parser is not None
        return self.parser.parser.CurrentLineNumber, self.parser.parser.CurrentByteIndex

    def start(self, tag: str, attrs: dict[str, str]) -> None:
        line, offset = self._position()
        name = _local(tag)
        stack = self.stack
        if stack:
            parent = stack[-1]
            counts = parent.counts
            if counts is None:
                counts = parent.counts = {}
            index = counts.get(name, 0) + 1
            counts[name] = index
            path = f"{parent.path}/{name}[{index}]"
        else:
            path = f"/{name}"
        stack.append(_XmlFrame(name, path, attrs, len(stack) + 1, line, offset))

    def data(self, text: str) -> None:
        if self.stack:
            self.stack[-1].own_text.append(text)

    def end(self, tag: str) -> None:
        frame = self.stack.pop()
        own = (
            plain_title(self.budget.add("".join(frame.own_text))) if frame.own_text else ""
        )
        attributes = ""
        if frame.attrs:
            attributes = self.budget.add(
                ", ".join(f"{_local(key)}={value}" for key, value in frame.attrs.items())
            )
        name = frame.name + (f" ({attributes})" if attributes else "")
        parent = self.stack[-1] if self.stack else None
        if frame.counts is None:
            if parent is not None:
                parent.lines.append(f"{name}: {own}" if own else name)
                if not parent.hint and own and frame.name.casefold() in _NAME_KEYS:
                    parent.hint = own[:80]
            else:
                self.root_leaf, self.root_leaf_text = frame, own
            return
        lines = ([name] if attributes else []) + ([own] if own else []) + frame.lines
        if frame.depth > MAX_NESTED_SECTION_DEPTH and parent is not None:
            parent.lines.append(f"{name}: " + " ; ".join(lines))
            return
        line_end, offset = self._position()
        close = self.source.find(b">", offset)
        hint = frame.hint or next(
            (frame.attrs[key] for key in frame.attrs if _local(key).casefold() in _NAME_KEYS),
            "",
        )
        index_suffix = frame.path.rsplit("/", 1)[-1][len(frame.name) :]
        node = _XmlNode(
            path=frame.path,
            title=(
                f"{frame.name}: {plain_title(hint)[:80]}"
                if hint
                else frame.name + index_suffix
            )[:MAX_TITLE_CHARS],
            line_start=frame.line_start,
            line_end=line_end,
            byte_start=frame.byte_start,
            byte_end=close + 1 if close >= 0 else len(self.source),
            search_text="\n".join(lines),
            children=frame.nodes,
        )
        if parent is not None:
            parent.nodes.append(node)
        else:
            self.root = node


def parse_xml(raw: bytes) -> tuple[DocumentSection, ...]:
    text = decode_xml_document(raw)
    if not text.strip():
        raise EmptyDocumentError("document contains no indexable text")
    data = text.encode("utf-8")
    collector = _XmlCollector(data)
    parser = SafeXmlParser(
        collector,
        label="XML document",
        max_elements=MAX_XML_DOCUMENT_ELEMENTS,
        encoding="UTF-8",
    )
    collector.parser = parser
    parser.parse(data)
    if collector.root is None:
        leaf = collector.root_leaf
        if leaf is None or not collector.root_leaf_text:
            return ()
        return (
            DocumentSection(
                stable_key=f"element:{leaf.path}",
                title=leaf.name,
                kind="xml_element",
                level=1,
                locator=NativeLocator(
                    "xml-element",
                    1,
                    {
                        "path": leaf.path,
                        "line_start": 1,
                        "line_end": len(source_lines(text)),
                    },
                ),
                search_text=collector.root_leaf_text,
                text=text,
            ),
        )
    sections: list[DocumentSection] = []

    def slice_text(start: int, end: int) -> str:
        return data[start:end].decode("utf-8")

    def emit(node: _XmlNode, level: int) -> None:
        sections.append(
            DocumentSection(
                stable_key=f"element:{node.path}",
                title=node.title,
                kind="xml_element",
                level=level,
                locator=NativeLocator(
                    "xml-element",
                    1,
                    {
                        "path": node.path,
                        "line_start": node.line_start,
                        "line_end": node.line_end,
                    },
                ),
                search_text=node.search_text,
                text=slice_text(node.byte_start, node.byte_end),
            )
        )
        check_section_count(len(sections))
        children = node.children
        if len(children) <= MAX_GROUP_ITEMS:
            for child in children:
                emit(child, level + 1)
            return
        sizes = [child.byte_end - child.byte_start for child in children]
        for group in _size_groups(sizes):
            first, last = children[group.start], children[group.stop - 1]
            body = slice_text(first.byte_start, last.byte_end)
            sections.append(
                DocumentSection(
                    stable_key=f"elements:{first.path}:{last.path}",
                    title=f"{first.title} … {last.title}" if first is not last else first.title,
                    kind="xml_elements",
                    level=level + 1,
                    locator=NativeLocator(
                        "xml-elements",
                        1,
                        {
                            "parent": node.path,
                            "first": first.path,
                            "last": last.path,
                            "line_start": first.line_start,
                            "line_end": last.line_end,
                        },
                    ),
                    search_text=body,
                    text=body,
                )
            )
            check_section_count(len(sections))

    emit(collector.root, 1)
    return tuple(sections)
