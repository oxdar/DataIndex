"""Registry of the document formats this extension can index.

Each entry maps a source type (MIME type) to its reader, size limit and, for
formats that need a third-party parser, the optional extra that installs it.
Readers for optional formats import their parser lazily, so the extension still
loads and indexes every other format when an extra is not installed.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from dataindex_ext_documents.formats import data, epub, legacy, mail, office, opendocument, pdf, rtf, text
from dataindex_ext_documents.formats._base import (
    MAX_BINARY_DOCUMENT_BYTES,
    MAX_DOCUMENT_BYTES,
    DocumentSection,
)

DISTRIBUTION = "dataindex-ext-documents"


@dataclass(frozen=True, slots=True)
class FormatSpec:
    """One indexable format.

    ``binary`` formats accept only bytes and allow larger files; text formats
    must be UTF-8 and may also be passed as ``str``.
    """

    source_type: str
    label: str
    suffixes: tuple[str, ...]
    parse: Callable[[bytes], tuple[DocumentSection, ...]]
    binary: bool = False
    requires: tuple[str, ...] = ()
    extra: str | None = None

    @property
    def max_bytes(self) -> int:
        return MAX_BINARY_DOCUMENT_BYTES if self.binary else MAX_DOCUMENT_BYTES

    def missing_modules(self) -> tuple[str, ...]:
        return tuple(
            module for module in self.requires if importlib.util.find_spec(module) is None
        )

    def install_hint(self) -> str | None:
        if self.extra is None:
            return None
        return f'pip install "{DISTRIBUTION}[{self.extra}]"'


_PLAIN_TEXT_SUFFIXES = (
    ".txt", ".text", ".rst", ".log", ".yaml", ".yml", ".toml", ".ini", ".cfg"
)

FORMATS: tuple[FormatSpec, ...] = (
    FormatSpec("text/markdown", "Markdown", (".md", ".markdown"), text.parse_markdown),
    FormatSpec("text/plain", "plain text", _PLAIN_TEXT_SUFFIXES, text.parse_plain_text),
    FormatSpec("text/html", "HTML", (".html", ".htm", ".xhtml"), text.parse_html),
    FormatSpec(
        "application/pdf", "PDF", (".pdf",), pdf.parse_pdf,
        binary=True, requires=("pypdf",), extra="pdf",
    ),
    FormatSpec(
        "application/msword", "Word 97-2003 document", (".doc",), legacy.parse_doc,
        binary=True, requires=("olefile",), extra="legacy-office",
    ),
    FormatSpec(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "Word document", (".docx",), office.parse_docx, binary=True,
    ),
    FormatSpec(
        "application/vnd.ms-excel", "Excel 97-2003 workbook", (".xls",), legacy.parse_xls,
        binary=True, requires=("xlrd",), extra="legacy-office",
    ),
    FormatSpec(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "Excel workbook", (".xlsx",), office.parse_xlsx, binary=True,
    ),
    FormatSpec(
        "application/vnd.ms-powerpoint", "PowerPoint 97-2003 presentation", (".ppt",),
        legacy.parse_ppt, binary=True, requires=("olefile",), extra="legacy-office",
    ),
    FormatSpec(
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "PowerPoint presentation", (".pptx",), office.parse_pptx, binary=True,
    ),
    FormatSpec(
        "application/vnd.oasis.opendocument.text", "OpenDocument text", (".odt",),
        opendocument.parse_odt, binary=True,
    ),
    FormatSpec(
        "application/vnd.oasis.opendocument.spreadsheet", "OpenDocument spreadsheet",
        (".ods",), opendocument.parse_ods, binary=True,
    ),
    FormatSpec(
        "application/vnd.oasis.opendocument.presentation", "OpenDocument presentation",
        (".odp",), opendocument.parse_odp, binary=True,
    ),
    FormatSpec("application/rtf", "Rich Text Format", (".rtf",), rtf.parse_rtf, binary=True),
    FormatSpec("application/epub+zip", "EPUB book", (".epub",), epub.parse_epub, binary=True),
    FormatSpec("message/rfc822", "email message", (".eml",), mail.parse_eml, binary=True),
    FormatSpec("text/csv", "CSV", (".csv",), data.parse_csv),
    FormatSpec("text/tab-separated-values", "TSV", (".tsv",), data.parse_tsv),
    FormatSpec("application/json", "JSON", (".json",), data.parse_json),
    FormatSpec("application/x-ndjson", "JSON Lines", (".jsonl", ".ndjson"), data.parse_jsonl),
    FormatSpec("application/xml", "XML", (".xml",), data.parse_xml),
)

FORMATS_BY_SOURCE_TYPE: Mapping[str, FormatSpec] = {
    spec.source_type: spec for spec in FORMATS
}
SOURCE_TYPES: tuple[str, ...] = tuple(spec.source_type for spec in FORMATS)
SUFFIXES: Mapping[str, str] = {
    suffix: spec.source_type for spec in FORMATS for suffix in spec.suffixes
}

__all__ = [
    "FORMATS",
    "FORMATS_BY_SOURCE_TYPE",
    "SOURCE_TYPES",
    "SUFFIXES",
    "FormatSpec",
]
