"""Hardened XML parsing on the standard library's expat binding.

DTD internal subsets and entity declarations are refused, which rules out
entity-expansion ("billion laughs") and external-entity attacks regardless of
the expat version Python was built with. External DTDs are never fetched.
Element depth and count are capped so parsing time and memory stay bounded.
"""

from __future__ import annotations

import codecs
import re
import xml.etree.ElementTree as ET
from typing import Protocol
from xml.parsers import expat

from dataindex_ext_documents.exceptions import (
    DocumentTooLargeError,
    InvalidDocumentEncodingError,
    MalformedDocumentError,
)
from dataindex_ext_documents.formats._base import MAX_MARKUP_ELEMENTS, ElementBudget

MAX_XML_DEPTH = 256
MAX_XML_ELEMENTS = MAX_MARKUP_ELEMENTS
MAX_TREE_ELEMENTS = 200_000

_DECLARED_ENCODING = re.compile(
    rb"<\?xml[^>]{0,200}?encoding\s*=\s*[\"']([A-Za-z0-9._-]{1,40})[\"']"
)


class XmlHandler(Protocol):
    def start(self, tag: str, attrs: dict[str, str]) -> None: ...

    def end(self, tag: str) -> None: ...

    def data(self, text: str) -> None: ...


def _qualified(name: str) -> str:
    # expat reports "uri}local" with this separator; ElementTree spells "{uri}local".
    return "{" + name if "}" in name else name


class SafeXmlParser:
    """An expat parser that refuses DTD subsets and bounds depth and size.

    ``max_elements`` bounds this one parse; ``budget`` is shared by every part of
    a package so the parts together stay bounded too.
    """

    def __init__(
        self,
        handler: XmlHandler,
        *,
        label: str,
        max_elements: int = MAX_XML_ELEMENTS,
        encoding: str | None = None,
        budget: ElementBudget | None = None,
    ) -> None:
        self.label = label
        self._handler = handler
        self._max_elements = max_elements
        self._budget = budget
        self.depth = 0
        self.elements = 0
        parser = expat.ParserCreate(encoding, namespace_separator="}")
        parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
        parser.buffer_text = True
        parser.buffer_size = 65536
        parser.ordered_attributes = False
        parser.StartElementHandler = self._start
        parser.EndElementHandler = self._end
        parser.CharacterDataHandler = handler.data
        parser.StartDoctypeDeclHandler = self._doctype
        parser.EntityDeclHandler = self._entity
        parser.ExternalEntityRefHandler = self._external_entity
        self.parser = parser

    def parse(self, data: bytes) -> None:
        try:
            self.parser.Parse(data, True)
        except expat.ExpatError as error:
            raise MalformedDocumentError(
                f"{self.label} is not well-formed XML "
                f"({expat.ErrorString(error.code)} at line {error.lineno})"
            ) from error

    def _start(self, name: str, attrs: dict[str, str]) -> None:
        self.depth += 1
        self.elements += 1
        if self.depth > MAX_XML_DEPTH:
            raise DocumentTooLargeError(
                f"{self.label} nests elements deeper than {MAX_XML_DEPTH} levels"
            )
        if self.elements > self._max_elements:
            raise DocumentTooLargeError(
                f"{self.label} has more than {self._max_elements} XML elements"
            )
        budget = self._budget
        if budget is not None:
            budget.used += 1
            if budget.used > budget.limit:
                raise DocumentTooLargeError(
                    f"{self.label} has more than {budget.limit} XML elements in all"
                )
        if attrs:
            attrs = {_qualified(key): value for key, value in attrs.items()}
        self._handler.start("{" + name if "}" in name else name, attrs)

    def _end(self, name: str) -> None:
        self.depth -= 1
        self._handler.end("{" + name if "}" in name else name)

    def _doctype(
        self,
        name: str | None,
        system_id: str | None,
        public_id: str | None,
        has_internal_subset: bool,
    ) -> None:
        if has_internal_subset:
            raise MalformedDocumentError(
                f"{self.label} declares an inline DTD, which is refused for safety"
            )

    def _entity(self, *args: object) -> None:
        raise MalformedDocumentError(
            f"{self.label} declares XML entities, which are refused for safety"
        )

    def _external_entity(self, *args: object) -> int:
        raise MalformedDocumentError(
            f"{self.label} references an external entity, which is refused for safety"
        )


class _TreeHandler:
    def __init__(self) -> None:
        self.builder = ET.TreeBuilder()

    def start(self, tag: str, attrs: dict[str, str]) -> None:
        self.builder.start(tag, attrs)

    def end(self, tag: str) -> None:
        self.builder.end(tag)

    def data(self, text: str) -> None:
        self.builder.data(text)


def parse_tree(
    data: bytes,
    *,
    label: str,
    max_elements: int = MAX_TREE_ELEMENTS,
    budget: ElementBudget | None = None,
) -> ET.Element:
    """Parse a small metadata part (relationships, styles, manifests) into a tree."""
    handler = _TreeHandler()
    SafeXmlParser(handler, label=label, max_elements=max_elements, budget=budget).parse(
        data
    )
    return handler.builder.close()


def stream_xml(
    data: bytes,
    handler: XmlHandler,
    *,
    label: str,
    max_elements: int = MAX_XML_ELEMENTS,
    budget: ElementBudget | None = None,
) -> None:
    """Parse a content part through ``handler`` without building a tree."""
    SafeXmlParser(handler, label=label, max_elements=max_elements, budget=budget).parse(
        data
    )


def decode_xml_document(raw: bytes) -> str:
    """Decode a standalone XML file using its byte-order mark or declaration."""
    if raw.startswith(b"\xef\xbb\xbf"):
        codec, raw = "utf-8", raw[3:]
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        codec = "utf-16"
    else:
        declared = _DECLARED_ENCODING.match(raw[:300])
        codec = declared.group(1).decode("ascii") if declared else "utf-8"
        try:
            canonical = codecs.lookup(codec).name
        except LookupError as error:
            raise InvalidDocumentEncodingError(
                f"XML declares the unknown encoding {codec!r}"
            ) from error
        if canonical.startswith(("utf-16", "utf-32")):
            raise InvalidDocumentEncodingError(
                "UTF-16 and UTF-32 XML must start with a byte-order mark"
            )
    try:
        text = raw.decode(codec)
    except UnicodeDecodeError as error:
        raise InvalidDocumentEncodingError(f"XML is not valid {codec} text") from error
    return text[1:] if text.startswith("\ufeff") else text
