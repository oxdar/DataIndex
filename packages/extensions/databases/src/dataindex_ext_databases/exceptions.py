"""Explicit errors raised by the free database extension."""


class DatabaseIndexError(Exception):
    """Base class for expected database extension failures."""


class UnsupportedDatabaseSourceError(DatabaseIndexError):
    """The source is not a supported SQLite database."""


class DatabaseRegistrationError(DatabaseIndexError):
    """The host could not register an authorized SQLite source."""


class InvalidDatabaseConfigurationError(DatabaseIndexError):
    """An allowlist or approved query definition is invalid."""


class DatabaseNotRegisteredError(DatabaseIndexError):
    """The requested source revision has not been registered."""


class DatabaseSourceConflictError(DatabaseIndexError):
    """A source ID/version was registered with conflicting configuration."""


class DatabaseSourceChangedError(DatabaseIndexError):
    """The registered SQLite file identity changed without a new source version."""


class UnauthorizedDatabaseAccessError(DatabaseIndexError):
    """A query attempts to access a table, column or join outside its allowlist."""


class DatabaseSchemaChangedError(DatabaseIndexError):
    """The live database schema changed after its metadata was indexed."""


class InvalidDatabaseToolRequestError(DatabaseIndexError):
    """A database query request is malformed or names an unknown template."""


class DatabaseDeadlineExceededError(DatabaseIndexError):
    """A database operation exceeded its cooperative deadline."""


class DatabaseExecutionBudgetExceededError(DatabaseIndexError):
    """A database query exceeded the fixed SQLite virtual-machine budget."""


class DatabaseQueryError(DatabaseIndexError):
    """SQLite rejected or failed to execute an approved query."""


class DatabaseResultError(DatabaseIndexError):
    """A query returned a value that cannot be safely represented as evidence."""


class DatabaseResultTooLargeError(DatabaseIndexError):
    """A query result exceeded the configured evidence-size limit."""


class ClosedDatabaseIndexError(DatabaseIndexError):
    """The database extension has been closed."""
