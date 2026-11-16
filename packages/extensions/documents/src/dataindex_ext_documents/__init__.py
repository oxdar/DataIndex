"""Free document extension for DataIndex: text, office, PDF, data and e-book files."""

from dataindex_ext_documents.exceptions import (
    ClosedDocumentIndexError,
    DocumentDeadlineExceededError,
    DocumentIndexError,
    DocumentNotRegisteredError,
    DocumentReaderUnavailableError,
    DocumentSectionNotFoundError,
    DocumentSourceConflictError,
    DocumentTooLargeError,
    EmptyDocumentError,
    InvalidDocumentEncodingError,
    InvalidDocumentToolRequestError,
    MalformedDocumentError,
    NoDocumentMatchError,
    ProtectedDocumentError,
    UnsupportedDocumentFormatError,
)
from dataindex_ext_documents.plugin import DocumentIndexExtension

__all__ = [
    "ClosedDocumentIndexError",
    "DocumentDeadlineExceededError",
    "DocumentIndexError",
    "DocumentIndexExtension",
    "DocumentNotRegisteredError",
    "DocumentReaderUnavailableError",
    "DocumentSectionNotFoundError",
    "DocumentSourceConflictError",
    "DocumentTooLargeError",
    "EmptyDocumentError",
    "InvalidDocumentEncodingError",
    "InvalidDocumentToolRequestError",
    "MalformedDocumentError",
    "NoDocumentMatchError",
    "ProtectedDocumentError",
    "UnsupportedDocumentFormatError",
]
