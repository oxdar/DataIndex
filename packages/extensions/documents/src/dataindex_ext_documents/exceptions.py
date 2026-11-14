"""Explicit errors raised by the free document extension."""


class DocumentIndexError(Exception):
    """Base class for expected document indexing failures."""


class UnsupportedDocumentFormatError(DocumentIndexError):
    """The source MIME type is not supported by this extension."""


class DocumentTooLargeError(DocumentIndexError):
    """A source exceeds the configured document or section limit."""


class InvalidDocumentEncodingError(DocumentIndexError):
    """A source is not valid UTF-8 text."""


class EmptyDocumentError(DocumentIndexError):
    """A source contains no indexable text."""


class DocumentSourceConflictError(DocumentIndexError):
    """A source ID/version was registered with different content."""


class DocumentNotRegisteredError(DocumentIndexError):
    """The requested source ID/version is not registered in this instance."""


class NoDocumentMatchError(DocumentIndexError):
    """No indexed section matches the deterministic keyword query."""


class DocumentSectionNotFoundError(DocumentIndexError):
    """The requested node ID is not a section of the registered document."""


class InvalidDocumentToolRequestError(DocumentIndexError):
    """A document retrieval request is malformed or names an unknown tool."""


class DocumentDeadlineExceededError(DocumentIndexError):
    """A document operation exceeded its cooperative deadline."""


class ClosedDocumentIndexError(DocumentIndexError):
    """The document extension has been closed."""
