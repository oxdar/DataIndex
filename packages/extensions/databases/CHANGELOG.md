# Changelog

## 0.1.0

- Initial independently installable SQLite schema and bounded-query extension.
- Added `register_database_config` (register from the JSON form of the approved
  columns and queries), `database_source_version` and `unregister_database`,
  which the local-mode SDK uses to persist and restore `submit_database`.
- The read authorizer allows SQLite's empty-column table read, which an approved
  `COUNT(*)` query produces, on approved tables only. Column reads are still
  checked against the approved columns.
- `DatabaseExtension` (`SQLiteDatabaseExtension` remains an alias) adds DuckDB
  files and database servers: PostgreSQL, MySQL, MariaDB, SQL Server, Oracle
  Database and MongoDB (integration tier) and Snowflake, BigQuery and ClickHouse
  (experimental). SQL servers use SQLAlchemy Core; DuckDB and MongoDB use their
  native drivers. Each engine is an optional extra (`duckdb`, `postgresql`,
  `mysql`, `mssql`, `oracle`, `snowflake`, `bigquery`, `clickhouse`, `mongodb`,
  `all`); the base install still has no driver dependency.
- Added `register_connection`, `register_connection_config`,
  `connection_source_version`, `describe_connection`, `supported_engines` and
  `parse_connection`. Connection URLs never hold a password: `password_env`
  names a `DATAINDEX_*` environment variable read only when a connection opens,
  and each engine accepts only an allowlist of URL parameters.
- Every server operation uses a fresh connection with the engine's read-only
  transaction, statement and lock timeouts where the engine has them, and is
  always rolled back. Server sources are versioned by their reflected schema.
- The `mysql` extra installs `PyMySQL[rsa]` for MySQL 8's default
  `caching_sha2_password` login.
- Views can be allowlisted on every engine and are indexed with
  `object_kind: "view"`. A row query may order by every selected column instead
  of every primary key, which is how keyless views are queried. SQLite views
  are compiled at registration and may then read only what was recorded;
  DuckDB view definitions and generated columns are parsed and may read only
  `main`-schema tables through consistent built-in functions; MongoDB views
  are versioned by their `viewOn` and `pipeline`.
- The `duckdb` extra now needs DuckDB 1.2 or later: DuckDB 1.1 fails to compare
  a `DATE` column with a text filter value.
