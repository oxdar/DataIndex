"""Parse submitted document bytes into sections and outline nodes.

Format detection is by the declared source type only; each format's reader lives
in :mod:`dataindex_ext_documents.formats`. This module validates the input size
and encoding, runs the reader, and builds the document hierarchy.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from dataindex.hierarchy import build_hierarchy
from dataindex.models import NativeLocator, Node, SourceRef
from dataindex_ext_documents.exceptions import (
    DocumentTooLargeError,
    EmptyDocumentError,
    InvalidDocumentEncodingError,
    MalformedDocumentError,
    UnsupportedDocumentFormatError,
)
from dataindex_ext_documents.formats import (
    FORMATS_BY_SOURCE_TYPE,
    SOURCE_TYPES,
    FormatSpec,
)
from dataindex_ext_documents.formats._base import (
    MAX_BINARY_DOCUMENT_BYTES,
    MAX_DOCUMENT_BYTES,
    MAX_DOCUMENT_EVENTS,
    MAX_DOCUMENT_LINES,
    MAX_DOCUMENT_SECTIONS,
    MAX_EXTRACTED_CHARS,
    MAX_TITLE_CHARS,
    DocumentSection,
)

MAX_QUERY_LENGTH = 256

SUPPORTED_SOURCE_TYPES = frozenset(SOURCE_TYPES)


@dataclass(frozen=True, slots=True)
class IndexedDocument:
    source: SourceRef
    digest: str
    sections: tuple[DocumentSection, ...]
    nodes: tuple[Node, ...]


def format_for(source_type: str) -> FormatSpec:
    spec = FORMATS_BY_SOURCE_TYPE.get(source_type)
    if spec is None:
        raise UnsupportedDocumentFormatError(
            f"unsupported document source type {source_type!r}"
        )
    return spec


def document_bytes(spec: FormatSpec, content: bytes | str) -> bytes:
    """The submitted bytes, after the type and size checks for ``spec``."""
    if isinstance(content, str):
        if spec.binary:
            raise TypeError(f"{spec.label} content must be bytes")
        try:
            encoded = content.encode("utf-8")
        except UnicodeEncodeError as error:
            raise InvalidDocumentEncodingError("document must be valid UTF-8") from error
    elif isinstance(content, bytes):
        encoded = content
    else:
        raise TypeError("document content must be bytes or str")
    if len(encoded) > spec.max_bytes:
        raise DocumentTooLargeError(
            f"document exceeds the {spec.max_bytes}-byte limit"
        )
    return encoded


def decode_document(content: bytes | str) -> str:
    """Decode a UTF-8 text document within the text-format size limit."""
    raw = document_bytes(format_for("text/plain"), content)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InvalidDocumentEncodingError("document must be valid UTF-8") from error


def parse_document(source: SourceRef, content: bytes | str) -> IndexedDocument:
    spec = format_for(source.source_type)
    raw = document_bytes(spec, content)
    sections = spec.parse(raw)
    if not sections:
        raise EmptyDocumentError("document contains no indexable text")
    if len(sections) > MAX_DOCUMENT_SECTIONS:
        raise DocumentTooLargeError(
            f"document exceeds the {MAX_DOCUMENT_SECTIONS}-section limit"
        )
    keys = {locator_key(section.locator) for section in sections}
    if len(keys) != len(sections):
        raise MalformedDocumentError("document produced duplicate section locators")
    return IndexedDocument(
        source=source,
        digest=hashlib.sha256(raw).hexdigest(),
        sections=sections,
        nodes=_build_nodes(source, sections),
    )


def _build_nodes(
    source: SourceRef, sections: tuple[DocumentSection, ...]
) -> tuple[Node, ...]:
    nodes: list[Node] = []
    levels: list[int] = []
    for section in sections:
        identity = "\0".join(
            (source.source_id, section.locator.scheme, section.stable_key)
        )
        node_id = "doc-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        nodes.append(
            Node(
                node_id=node_id,
                source_id=source.source_id,
                source_version=source.version,
                kind=section.kind,
                title=section.title,
                locator=section.locator,
            )
        )
        levels.append(section.level)
    return build_hierarchy(nodes, levels)


def locator_key(locator: NativeLocator) -> str:
    value = (
        locator.scheme,
        locator.locator_version,
        tuple(sorted(locator.value.items())),
    )
    return repr(value)


__all__ = [
    "MAX_BINARY_DOCUMENT_BYTES",
    "MAX_DOCUMENT_BYTES",
    "MAX_DOCUMENT_EVENTS",
    "MAX_DOCUMENT_LINES",
    "MAX_DOCUMENT_SECTIONS",
    "MAX_EXTRACTED_CHARS",
    "MAX_QUERY_LENGTH",
    "MAX_TITLE_CHARS",
    "SUPPORTED_SOURCE_TYPES",
    "DocumentSection",
    "IndexedDocument",
    "decode_document",
    "document_bytes",
    "format_for",
    "locator_key",
    "parse_document",
]
