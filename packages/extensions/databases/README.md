# dataindex-ext-databases

An independently installable DataIndex extension for database files and
database servers. It indexes allowlisted table, view, column, primary-key and
foreign-key metadata (never rows), then performs bounded live reads using
host-approved typed query definitions. Models and callers choose an approved
query ID and filter values; they cannot send SQL.

## Supported engines

| Engine | Connect with | Install extra | Driver (license) | Tier |
| --- | --- | --- | --- | --- |
| SQLite | file path | none | Python `sqlite3` | tested |
| DuckDB | file path | `duckdb` | duckdb (MIT) | tested |
| PostgreSQL | `postgresql://`, `postgres://` | `postgresql` | psycopg 3 (LGPL-3.0) | integration |
| MySQL | `mysql://` | `mysql` | PyMySQL (MIT) | integration |
| MariaDB | `mariadb://` | `mysql` | PyMySQL (MIT) | integration |
| SQL Server | `mssql://`, `sqlserver://` | `mssql` | pyodbc (MIT-0) and the Microsoft ODBC Driver 17 or 18 | integration |
| Oracle Database | `oracle://` | `oracle` | python-oracledb, thin mode (Apache-2.0 or UPL-1.0) | integration |
| MongoDB | `mongodb://`, `mongodb+srv://` | `mongodb` | PyMongo (Apache-2.0) | integration |
| Snowflake | `snowflake://` | `snowflake` | snowflake-sqlalchemy (Apache-2.0) | experimental |
| BigQuery | `bigquery://` | `bigquery` | sqlalchemy-bigquery (MIT) | experimental |
| ClickHouse | `clickhouse://` | `clickhouse` | clickhouse-connect (Apache-2.0) | experimental |

SQL servers are reached through SQLAlchemy Core (MIT), which every server extra
installs. Any other URL scheme is refused.

- **tested:** exercised in-process by every test run.
- **integration:** the shared contract also runs in every test run through
  stand-ins (a SQLite-backed SQLAlchemy engine for the SQL servers and
  `mongomock` for MongoDB), and a live contract test (`test_live_*`) runs
  against a real server in the `database-servers` CI job: PostgreSQL 16,
  MySQL 8.4, MariaDB 11.4, SQL Server 2022, Oracle Database 23ai Free and
  MongoDB 7.
- **experimental:** the URL rules, driver and guards are implemented, but
  nothing is tested against the real service. Check them with your own
  read-only account before relying on them.

`DatabaseExtension.supported_engines()` returns this list with each engine's
URL schemes, extra and tier.

## Install

```powershell
python -m pip install dataindex-core dataindex-ext-databases      # SQLite only
python -m pip install "dataindex-ext-databases[postgresql]"       # add one engine
python -m pip install "dataindex-ext-databases[all]"              # every driver
```

The base install has no runtime dependency beyond `dataindex-core`. Each
driver is imported only when its engine is used; if it is missing, that engine
fails with `DatabaseDriverUnavailableError` and the matching `pip install`
command while other engines keep working. Notes on specific extras:

- `postgresql` installs psycopg, which is LGPL-3.0; its binary wheel bundles
  libpq.
- `mssql` also needs the Microsoft ODBC Driver 17 or 18 for SQL Server, a
  system package that pip cannot install.
- `mysql` installs `PyMySQL[rsa]`, which adds `cryptography` for the default
  `caching_sha2_password` login of MySQL 8.
- `duckdb` needs DuckDB 1.2 or later; DuckDB 1.1 cannot compare a `DATE`
  column with a text filter value.
- Other drivers are accepted when installed separately: `postgresql+psycopg2`,
  `postgresql+pg8000`, `mysql+mysqldb`, `mysql+mysqlconnector`,
  `mariadb+mysqldb`, `mariadb+mariadbconnector` and `mssql+pymssql`.

Tables and views can both be allowlisted; see [Views](#views). The extension
does not support virtual tables in SQLite and DuckDB files, arbitrary SQL,
writes or schema migration.

## Connecting to a database server

A connection is a URL **without a password**, an optional `password_env` that
names the environment variable holding the password, and an optional `schema`:

```powershell
$env:DATAINDEX_SALES_PASSWORD = "..."
dataindex submit-db --url postgresql://reader@db.example.com:5432/sales `
  --password-env DATAINDEX_SALES_PASSWORD --schema public --config sales.json
```

```python
client.submit_database(
    url="postgresql://reader@db.example.com:5432/sales",
    password_env="DATAINDEX_SALES_PASSWORD",
    schema="public",
    allowed_columns={"invoices": ["invoice_id", "amount_cents", "status"]},
    queries=[...],
)
```

- A URL with `user:password@` is refused. `password_env` must match
  `DATAINDEX_[A-Za-z0-9_]+` (at most 128 characters). The variable is read only
  when a connection opens; only its name is stored. Error messages are scrubbed
  of the password and name only the password-free URL.
- Each engine accepts only the URL parameters below. Driver options such as
  MySQL `init_command` or `local_infile`, SQL Server `odbc_connect` or inline
  BigQuery credentials could run statements, read local files or replace the
  reviewed connection, so they are refused.
- SQLite and DuckDB databases are files: submit their path. `sqlite://` and
  `duckdb://` URLs are refused.

| Engine | Example URL | Accepted URL parameters |
| --- | --- | --- |
| PostgreSQL | `postgresql://reader@db.example.com:5432/sales?sslmode=verify-full` | `sslmode`, `sslrootcert`, `sslcert`, `sslkey`, `application_name`, `target_session_attrs` |
| MySQL, MariaDB | `mysql://reader@db.example.com:3306/sales`, `mariadb://reader@db.example.com/sales` | `charset`, `ssl_ca`, `ssl_check_hostname`, `ssl_verify_cert` |
| SQL Server | `mssql://reader@db.example.com:1433/sales?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes` | `driver`, `Encrypt`, `TrustServerCertificate`, `ApplicationIntent`, `Trusted_Connection`; pyodbc connections always use `ApplicationIntent=ReadOnly` |
| Oracle Database | `oracle://reader@db.example.com:1521?service_name=SALES` | `service_name` |
| MongoDB | `mongodb://reader@db1.example.com,db2.example.com/sales?replicaSet=rs0&authSource=admin`, `mongodb+srv://reader@cluster.example.com/sales` | `authSource`, `authMechanism`, `tls`, `tlsCAFile`, `replicaSet`, `readPreference`, `directConnection`, `appName`; the database is required |
| Snowflake | `snowflake://reader@myorg-account/SALES/PUBLIC?warehouse=WH&role=READER` | `warehouse`, `role` |
| BigQuery | `bigquery://my-project/sales?maximum_bytes_billed=1000000000` | `location`, `maximum_bytes_billed` (required); no user name or password, the environment's default Google credentials are used |
| ClickHouse | `clickhouse://reader@ch.example.com:8443/sales?secure=true` | `secure`, `verify`; no `schema` |

MongoDB, BigQuery and ClickHouse do not take a `schema`; elsewhere the login's
default schema is used when it is omitted.

## Read-only guards

Every operation on a server opens one fresh, unpooled connection, applies the
engine's guards, and always rolls back. Connecting times out after 10 seconds
and each statement after 30 seconds or the caller's deadline, whichever is
sooner.

| Engine | Guards |
| --- | --- |
| PostgreSQL | read-only transaction (checked), `statement_timeout`, `lock_timeout` |
| MySQL, MariaDB | read-only transaction (checked), `max_execution_time` or `max_statement_time`, read timeout, `LOCAL INFILE` off |
| Oracle Database | read-only transaction, call timeout |
| SQL Server | `ApplicationIntent=ReadOnly` (pyodbc only; `mssql+pymssql` cannot set it), lock timeout, query timeout |
| Snowflake | `STATEMENT_TIMEOUT_IN_SECONDS` |
| BigQuery | `maximum_bytes_billed` cost cap; no server-side statement timeout |
| ClickHouse | `max_execution_time` when the client exposes it |
| MongoDB | only `find` and fixed `aggregate` pipelines, `maxTimeMS` on each operation, no disk use for sorting or grouping |
| DuckDB | read-only file, external access and extension loading off, one thread, 256 MB memory, interrupted at the deadline |
| SQLite | see below |

SQL Server, Snowflake, BigQuery and ClickHouse have no read-only transaction
mode. **Always connect with a login that can only read the allowlisted
tables**; on those engines the login is the only barrier to writes, and on the
others the guards are defense in depth, not a replacement. The same holds for
allowlisted server views: the server decides what a view runs and with whose
privileges.

## Schema versions and freshness

Server tables and views listed in `allowed_columns` are reflected at
registration. The source version is a digest of their reflected schema
(`<engine>-schema:...`); DataIndex local mode appends a digest of the approved
queries. Rows are never indexed, and row changes do not change the version:
query results are live reads, and each evidence record carries its
`read_timestamp_utc`. The same question asked twice may get different values.
For a server view the digest covers its columns and their types but not its
SQL text, so redefining a view without changing its columns changes live
results the way a row change does, without changing the version.

The plugin reflects the schema again whenever it builds index records. DataIndex
local mode also reflects it each time a server source is listed, opened or
queried, so a changed schema reports `stale` and blocks queries until the
connection is submitted again, and an unreachable server, missing driver or
unset password variable reports `unavailable`. Each of those calls therefore
makes a round trip to the server. Hosts calling the plugin directly can compare
`connection_source_version(config)` with the registered version.

SQLite and DuckDB file versions follow the file state instead (see below).

### MongoDB

A collection plays the role of a table, and allowlisted dotted leaf field paths
(`status`, `customer.country`) play the role of columns; `_id` is the primary
key of a collection. Declared types come from the collection's `$jsonSchema`
validator when it has one. Joins are not supported, and `system.` collections
and paths that start with `$` cannot be allowlisted. Queries are generated
`find` filters or fixed `$match`/`$group`/`$sort`/`$limit` pipelines, so no
caller-supplied operator, expression or `$where` code reaches the server.

### DuckDB

A DuckDB file is handled like a SQLite file: the host submits its local path,
only tables and views in the file's `main` schema can be allowlisted, and the
source version follows the state of the file and its `.wal`.

## Views

Views can be allowlisted wherever tables can: in SQLite and DuckDB files, on
the SQL servers (materialized views too) and in MongoDB. Index records mark
them with `object_kind: "view"`, and queries read them with the same generated,
bound-parameter statements as tables.

- **Ordering.** A row query must order by the primary key of every table it
  reads, or by every selected column. Views have no primary key, so their row
  queries order by every selected column. That order is deterministic because
  rows that tie are identical, except for values the column's collation treats
  as equal (case or accents under an insensitive collation, `-0.0` and `0.0`),
  which can come back in either order. `COUNT` and `SUM` queries need no
  ordering.
- **Joins** still need an allowlisted single-column declared foreign key.
  Views rarely have one, so a view is usually queried on its own.
- **SQLite files.** Registration compiles each allowlisted view with `EXPLAIN`,
  without reading rows, and records every table, column and function it uses.
  A view may read only ordinary tables and other views in the `main` schema:
  views over `sqlite_schema`, virtual tables such as FTS5, or table-valued
  functions such as `pragma_table_info` are refused, and `trusted_schema` stays
  off. At query time the SQLite authorizer allows, inside each view, exactly
  what registration recorded and nothing else. A SQLite library that does not
  report which view performs an access cannot register views.
- **DuckDB files.** Registration parses each view's stored definition with
  DuckDB's own parser (`json_serialize_sql`) and follows nested views, up to
  64. A view may read only tables and views in the file's `main` schema,
  through joins, subqueries, common table expressions and `VALUES`, and may call
  only built-in scalar and aggregate functions DuckDB marks as consistent, plus
  the built-in window functions. Table functions (`range`, `read_csv`,
  `duckdb_settings()`), macros, schema-qualified calls, volatile functions
  (`now()`, `random()`), functions that read the session or local clock
  (`current_setting`, `getvariable`, `getenv`, `current_localtime`,
  `current_localtimestamp`), `USING SAMPLE` and time travel are refused.
  Generated columns are checked by the same rules, because DuckDB evaluates
  them whenever they are read: all generated columns of every table a view
  reads, and the allowlisted columns of allowlisted tables. DuckDB before 2.0
  does not report which columns are generated, so in a table that declares any
  generated column every column default is checked too, and a default such as
  `now()` makes the table unsupported there.
- **SQL servers.** Views and materialized views are reflected like tables. The
  server enforces the view's own permissions and runs it under its own rules,
  for example with the view owner's privileges in PostgreSQL unless the view
  sets `security_invoker`, and the extension cannot inspect what a server view
  runs. Allowlist only views the read-only login may read in full.
- **MongoDB.** Read-only views are allowlisted like collections, and the server
  runs a view's pipeline with the connecting user's privileges. `_id` is not a
  key in a view because the pipeline may drop or repeat it, and filter values
  are not converted to declared types because views have no validator.

A changed view definition is detected on SQLite and DuckDB files (the file
state changes, and DuckDB's schema digest covers view definitions) and on
MongoDB (the digest covers `viewOn` and `pipeline`), and the source must then
be registered again. On SQL servers only column changes are detected (see
[Schema versions and freshness](#schema-versions-and-freshness)).

## Authorization and query safety

The host authorizes the database source before registering it and passes its
explicit local path to `DatabaseExtension.register_database`, or its connection
to `DatabaseExtension.register_connection` (`SQLiteDatabaseExtension` remains
an alias). The extension never derives a path or server from a source ID, and
a `SourceRef.source_type` must match the file or engine
(`application/vnd.sqlite3`, `application/vnd.duckdb` or
`application/vnd.dataindex.db.<engine>`). Registration requires a
per-source table/column allowlist and one or more immutable `QueryDefinition`
objects. Tool requests name only an approved query ID and supply values for its
declared filters; they cannot supply SQL, table names, columns, operators or
join expressions.
For registered sources, `tools_for_source()` also publishes the approved query
IDs as a schema enum so a planner can discover valid operations without
guessing. The central DataIndex dispatcher still validates the request and
rechecks source authorization before execution.

On every engine, statements are generated from the approved definition with
bound parameters (SQLAlchemy Core expressions on servers). Joins must match an
allowlisted single-column declared foreign key; composite-FK joins are rejected,
and MongoDB has no joins. Only `COUNT` and `SUM` aggregates are available.
Results are bounded to at most 100 rows, 32 selected columns and 64 KiB of
serialized row data, with an additional cooperative deadline from the caller.

For SQLite files, every connection uses SQLite URI `mode=ro`, enables
`query_only`, disables trusted schema, applies an SQLite authorizer, and has
bounded lock and value limits. Queries are also limited to 100,000 SQLite
virtual-machine instructions. Reads and schema checks run within one SQLite
read transaction.

For shared multi-tenant databases, the host must include a
`FixedFilterRule` for each required source/tenant scope in every approved query.
These predicates are generated by the host and are combined with caller filters;
the tool request cannot override or omit them. This is a query-level scope
control, not a replacement for host authorization or database/file permissions.

The extension indexes no table rows. Schema records contain only allowlisted
table/column metadata and foreign-key nodes that point to their target table
node. Live query evidence includes the source ID/version, query ID, read
timestamp, schema version, canonical request filters, effective limit, query
definition digest, result rows, truncation status and operation, plus the
database file-state digest for files or the engine and `connection_id` (a
digest of the password-free URL and schema) for servers. Aggregates are marked
as calculated evidence; monetary example values use integer cents to avoid
floating-point totals.

For files, the host must use an immutable snapshot identifier as
`SourceRef.version`. Registration and reads detect ordinary changes to the main
database and WAL file identity (device, inode, size and modification time), but
this metadata digest is not a content hash and cannot prove that a file's bytes
were not rewritten while preserving those attributes. Up to 256 source revisions
can be registered at once; registering a new revision does not evict earlier
ones.

The caller remains responsible for source authorization, protected file
permissions, database logins, and selecting a source path or server. Read-only
modes are defense-in-depth, not a substitute for OS-level or database access
controls or the central DataIndex dispatcher.

### Hosts that persist the configuration

`register_database_config(source, database_path, config)` registers a database
file from a JSON-compatible configuration with exactly two keys:
`allowed_columns` (table name to column names) and `queries` (1 to 128
`QueryDefinition.to_mapping()` values, validated through
`QueryDefinition.from_mapping`). `register_connection_config(source, config,
expected_state=None)` does the same for a server with a third key,
`connection` (`url`, and optionally `password_env`, `schema` and `engine`); the
configuration never contains a password. A host can store either form and
register the source again after a restart; the DataIndex local mode does this
for `submit_database`.

- `database_source_version(path)` returns a `sqlite-state:` or `duckdb-state:`
  version string for the file's current state.
- `connection_source_version(config)` contacts the server and returns the
  current `<engine>-schema:` state without registering anything; passing it as
  `expected_state` makes registration fail with `DatabaseSourceChangedError`
  if the schema changed in between.
- `describe_connection(connection)` validates a connection without contacting
  the server and returns its canonical password-free form, source type, display
  name, support tier and `connection_id`.
- `unregister_database(source_id)` drops every registered revision of a source
  and returns how many were dropped.

A configuration with other keys, or an invalid definition or connection, raises
`InvalidDatabaseConfigurationError` (`InvalidDatabaseConnectionError` for
connections).

## Example

Run [`examples/basic_usage.py`](examples/basic_usage.py) from a source checkout.
It creates a temporary synthetic contracts/invoices database, registers only
approved schema and queries, then prints schema locators and cited aggregate
evidence. The SQL fixture is synthetic and contains no production data.

## License status

The public core/free-extension license has not yet been approved. The included
[`LICENSE`](LICENSE) file explicitly marks this distribution as not licensed for
redistribution until maintainer/legal review selects a license. Local builds
and tests do not authorize a release.
