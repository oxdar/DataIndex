"""Free database schema and bounded-query extension for DataIndex.

SQLite works with the standard library. DuckDB files and database servers
(PostgreSQL, MySQL, MariaDB, SQL Server, Oracle, Snowflake, BigQuery,
ClickHouse, MongoDB) need the matching optional extra.
"""

from dataindex_ext_databases.connections import (
    ConnectionSettings,
    parse_connection,
    supported_engines,
)
from dataindex_ext_databases.exceptions import (
    ClosedDatabaseIndexError,
    DatabaseConnectionError,
    DatabaseDeadlineExceededError,
    DatabaseDriverUnavailableError,
    DatabaseExecutionBudgetExceededError,
    DatabaseIndexError,
    DatabaseNotRegisteredError,
    DatabaseQueryError,
    DatabaseRegistrationError,
    DatabaseResultError,
    DatabaseResultTooLargeError,
    DatabaseSourceChangedError,
    DatabaseSchemaChangedError,
    DatabaseSourceConflictError,
    InvalidDatabaseConfigurationError,
    InvalidDatabaseConnectionError,
    InvalidDatabaseToolRequestError,
    UnauthorizedDatabaseAccessError,
    UnsupportedDatabaseSourceError,
)
from dataindex_ext_databases.plugin import DatabaseExtension, SQLiteDatabaseExtension
from dataindex_ext_databases.queries import (
    AggregateRule,
    ColumnRef,
    FixedFilterRule,
    FilterRule,
    JoinRule,
    QueryDefinition,
)

__all__ = [
    "AggregateRule",
    "ClosedDatabaseIndexError",
    "ColumnRef",
    "ConnectionSettings",
    "DatabaseConnectionError",
    "DatabaseDeadlineExceededError",
    "DatabaseDriverUnavailableError",
    "DatabaseExecutionBudgetExceededError",
    "DatabaseExtension",
    "DatabaseIndexError",
    "DatabaseNotRegisteredError",
    "DatabaseQueryError",
    "DatabaseRegistrationError",
    "DatabaseResultError",
    "DatabaseResultTooLargeError",
    "DatabaseSourceChangedError",
    "DatabaseSchemaChangedError",
    "DatabaseSourceConflictError",
    "FixedFilterRule",
    "FilterRule",
    "InvalidDatabaseConfigurationError",
    "InvalidDatabaseConnectionError",
    "InvalidDatabaseToolRequestError",
    "JoinRule",
    "QueryDefinition",
    "SQLiteDatabaseExtension",
    "UnauthorizedDatabaseAccessError",
    "UnsupportedDatabaseSourceError",
    "parse_connection",
    "supported_engines",
]
