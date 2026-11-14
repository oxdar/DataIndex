"""Free Markdown, plain-text and HTML extension for DataIndex."""

from dataindex_ext_documents.exceptions import (
    ClosedDocumentIndexError,
    DocumentDeadlineExceededError,
    DocumentIndexError,
    DocumentNotRegisteredError,
    DocumentSectionNotFoundError,
    DocumentSourceConflictError,
    DocumentTooLargeError,
    EmptyDocumentError,
    InvalidDocumentEncodingError,
    InvalidDocumentToolRequestError,
    NoDocumentMatchError,
    UnsupportedDocumentFormatError,
)
from dataindex_ext_documents.plugin import DocumentIndexExtension

__all__ = [
    "ClosedDocumentIndexError",
    "DocumentDeadlineExceededError",
    "DocumentIndexError",
    "DocumentIndexExtension",
    "DocumentNotRegisteredError",
    "DocumentSectionNotFoundError",
    "DocumentSourceConflictError",
    "DocumentTooLargeError",
    "EmptyDocumentError",
    "InvalidDocumentEncodingError",
    "InvalidDocumentToolRequestError",
    "NoDocumentMatchError",
    "UnsupportedDocumentFormatError",
]
