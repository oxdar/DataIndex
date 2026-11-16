"""Connection validation for database servers; no driver or network is used."""

from __future__ import annotations

import pytest

from dataindex.models import SourceRef
from dataindex_ext_databases import (
    DatabaseConnectionError,
    DatabaseDriverUnavailableError,
    DatabaseExtension,
    InvalidDatabaseConnectionError,
    UnsupportedDatabaseSourceError,
    parse_connection,
    supported_engines,
)
from dataindex_ext_databases import connections as connections_module
from dataindex_ext_databases.connections import scrub


@pytest.mark.parametrize(
    ("connection", "engine", "canonical"),
    [
        (
            {"url": "postgres://reader@db.example.com:5432/sales?sslmode=require"},
            "postgresql",
            "postgresql+psycopg://reader@db.example.com:5432/sales?sslmode=require",
        ),
        (
            {"url": "mysql://reader@db:3306/sales"},
            "mysql",
            "mysql+pymysql://reader@db:3306/sales",
        ),
        (
            {"url": "mariadb://reader@db/sales"},
            "mariadb",
            "mariadb+pymysql://reader@db/sales",
        ),
        (
            {
                "url": (
                    "sqlserver://reader@db:1433/sales"
                    "?driver=ODBC+Driver+18+for+SQL+Server"
                ),
                "schema": "dbo",
            },
            "mssql",
            (
                "mssql+pyodbc://reader@db:1433/sales?ApplicationIntent=ReadOnly"
                "&driver=ODBC%20Driver%2018%20for%20SQL%20Server"
            ),
        ),
        (
            {"url": "oracle://reader@db:1521?service_name=FREEPDB1"},
            "oracle",
            "oracle+oracledb://reader@db:1521?service_name=FREEPDB1",
        ),
        (
            {"url": "snowflake://reader@acct/SALES/PUBLIC?warehouse=WH"},
            "snowflake",
            "snowflake://reader@acct/SALES/PUBLIC?warehouse=WH",
        ),
        (
            {"url": "bigquery://project/analytics?maximum_bytes_billed=1000000"},
            "bigquery",
            "bigquery://project/analytics?maximum_bytes_billed=1000000",
        ),
        (
            {"url": "clickhouse://reader@ch:8443/default?secure=true"},
            "clickhouse",
            "clickhousedb://reader@ch:8443/default?secure=true",
        ),
        (
            {"url": "mongodb://reader@m1:27017,m2/app?replicaSet=rs0&authSource=admin"},
            "mongodb",
            "mongodb://reader@m1:27017,m2/app?authSource=admin&replicaSet=rs0",
        ),
    ],
)
def test_connections_are_canonical_and_password_free(
    connection: dict[str, str], engine: str, canonical: str
) -> None:
    settings = parse_connection(connection)

    assert settings.engine.name == engine
    assert settings.url == canonical
    assert settings.to_mapping()["url"] == canonical
    assert parse_connection(settings.to_mapping()).url == canonical


@pytest.mark.parametrize(
    ("connection", "message"),
    [
        ({"url": "postgresql://reader:secret@db/sales"}, "must not contain a password"),
        ({"url": "sqlite:///data.db"}, "submit the file by path"),
        ({"url": "duckdb:///data.duckdb"}, "submit the file by path"),
        ({"url": "db2://reader@db/sales"}, "unsupported database URL scheme"),
        ({"url": "postgresql+asyncpg://reader@db/sales"}, "unsupported PostgreSQL driver"),
        ({"url": "postgresql://reader@db/sales?options=-c"}, "is not allowed"),
        ({"url": "mysql://reader@db/sales?local_infile=1"}, "is not allowed"),
        (
            {"url": "postgresql://reader@db/sales", "password_env": "PGPASSWORD"},
            "DATAINDEX_",
        ),
        ({"url": "mongodb://reader@m1/app", "schema": "x"}, "separate schema"),
        ({"url": "bigquery://reader@project/analytics?maximum_bytes_billed=1"}, "default credentials"),
        ({"url": "bigquery://project/analytics"}, "maximum_bytes_billed"),
        (
            {"url": "mssql://reader@db/sales?ApplicationIntent=ReadWrite"},
            "ApplicationIntent=ReadOnly",
        ),
        ({"url": "postgresql://reader@db/sales#frag"}, "fragments"),
        ({"url": "postgresql://reader@db/sa les"}, "spaces"),
        ({"url": "postgresql://reader@db/sales?sslmode=a&sslmode=b"}, "repeated"),
        ({"url": "mysql://reader@a,b/sales"}, "exactly one host"),
        ({"url": "postgresql://reader@db/sales", "engine": "mysql"}, "does not match"),
        ({"url": "postgresql://reader@db/sales", "user": "x"}, "unsupported connection"),
    ],
)
def test_unsafe_or_malformed_connections_are_rejected(
    connection: dict[str, str], message: str
) -> None:
    with pytest.raises(InvalidDatabaseConnectionError, match=message):
        parse_connection(connection)


def test_password_is_read_from_its_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = parse_connection(
        {"url": "postgresql://reader@db/sales", "password_env": "DATAINDEX_PG_PW"}
    )
    monkeypatch.delenv("DATAINDEX_PG_PW", raising=False)
    with pytest.raises(DatabaseConnectionError, match="set the DATAINDEX_PG_PW"):
        settings.password()

    monkeypatch.setenv("DATAINDEX_PG_PW", "s3cr:et/pw")
    assert settings.password() == "s3cr:et/pw"
    assert "s3cr" not in repr(settings)
    assert "s3cr" not in str(settings.to_mapping())


def test_scrub_removes_plain_and_percent_encoded_passwords() -> None:
    message = scrub(
        RuntimeError("login failed for s3cr:et/pw at x://u:s3cr%3Aet%2Fpw@h\nline"),
        "s3cr:et/pw",
    )
    assert "s3cr" not in message
    assert message == "RuntimeError: login failed for *** at x://u:***@h line"
    assert len(scrub("x" * 1000, None)) == 300


def test_describe_connection_is_offline_and_reports_missing_drivers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension = DatabaseExtension()
    monkeypatch.setattr(connections_module, "_importable", lambda module: True)

    description = extension.describe_connection(
        {
            "url": "postgresql://reader@db/sales",
            "password_env": "DATAINDEX_PG_PW",
            "schema": "finance",
        }
    )
    other_schema = extension.describe_connection(
        {"url": "postgresql://reader@db/sales", "schema": "hr"}
    )

    assert description == {
        "connection": {
            "engine": "postgresql",
            "url": "postgresql+psycopg://reader@db/sales",
            "password_env": "DATAINDEX_PG_PW",
            "schema": "finance",
        },
        "engine": "postgresql",
        "label": "PostgreSQL",
        "source_type": "application/vnd.dataindex.db.postgresql",
        "display_name": "PostgreSQL db/sales.finance",
        "connection_id": description["connection_id"],
        "support_tier": "integration",
    }
    assert description["connection_id"] != other_schema["connection_id"]

    monkeypatch.setattr(connections_module, "_importable", lambda module: False)
    with pytest.raises(
        DatabaseDriverUnavailableError, match=r"dataindex-ext-databases\[oracle\]"
    ):
        extension.describe_connection({"url": "oracle://reader@db/FREEPDB1"})
    extension.close()


def test_server_and_file_registration_entry_points_are_not_interchangeable(
    tmp_path,
) -> None:
    extension = DatabaseExtension()
    server = SourceRef("db-1", "application/vnd.dataindex.db.postgresql", "v1")
    with pytest.raises(UnsupportedDatabaseSourceError, match="register_connection"):
        extension.register_database(
            server, tmp_path / "x.db", allowed_columns={"t": ("c",)}, queries=()
        )
    sqlite_source = SourceRef("db-2", "application/vnd.sqlite3", "v1")
    with pytest.raises(UnsupportedDatabaseSourceError, match="does not match"):
        extension.register_connection(
            sqlite_source,
            {"url": "postgresql://reader@db/sales"},
            allowed_columns={"t": ("c",)},
            queries=(),
        )
    extension.close()


def test_supported_engines_lists_every_server_engine_with_its_tier() -> None:
    engines = supported_engines()

    assert sorted(engines) == [
        "bigquery",
        "clickhouse",
        "mariadb",
        "mongodb",
        "mssql",
        "mysql",
        "oracle",
        "postgresql",
        "snowflake",
    ]
    assert {name for name, spec in engines.items() if spec["tier"] == "experimental"} == {
        "bigquery",
        "clickhouse",
        "snowflake",
    }
    assert DatabaseExtension.supported_engines() == engines
    source_types = set(DatabaseExtension().manifest().source_types)
    assert {
        f"application/vnd.dataindex.db.{name}" for name in engines
    } | {"application/vnd.duckdb", "application/vnd.sqlite3"} <= source_types
