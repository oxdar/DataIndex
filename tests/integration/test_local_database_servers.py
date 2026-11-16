"""Database servers and DuckDB files through the local client and the real plugin.

No database server runs here. A stand-in engine serves a SQLite file through the
plugin's SQLAlchemy server backend: the code path PostgreSQL, MySQL, SQL Server
and Oracle take, minus their session guards. Core accepts only source types the
installed plugin manifest declares, so the stand-in borrows the BigQuery engine
slot, which issues no guard statement. DuckDB files run in-process.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from dataindex import ConfigurationError, DataIndexClient, SourceRejectedError
from dataindex_ext_databases import connections as connections_module
from dataindex_ext_databases import parse_connection
from dataindex_ext_databases.connections import EngineSpec
from examples.mixed_sources import DATABASE_FIXTURE
from local_flow_helpers import (
    DB_CLAIM,
    DB_QUESTION,
    OVERDUE_FILTERS,
    OVERDUE_QUERY_ID,
    ScriptedModel,
    call_json,
    database_arguments,
    database_policy,
    make_database,
    overdue_totals,
    rows_by_contract,
)

STAND_IN_ENGINE = "bigquery"
SERVER_TYPE = f"application/vnd.dataindex.db.{STAND_IN_ENGINE}"
SERVER_URL = "sqlite+pysqlite:///server.db"
LABEL = "SQLite stand-in"
SECRET = "Sup3r-s3cret-pa55word"
PASSWORD_ENV = "DATAINDEX_TEST_STAND_IN_PASSWORD"
UNREACHABLE = (
    "the database server could not be checked; make sure it is reachable, its "
    "driver is installed and its password environment variable is set"
)
SCHEMA_CHANGED = (
    "database schema or approved queries changed after it was submitted; "
    "submit it again"
)
CHANGED = (
    "the document's file is missing or changed after it was submitted, so it "
    "cannot be read"
)
UNAVAILABLE = (
    "that tool cannot be used with this document, or its plugin is unavailable"
)
EXPECTED_TOTALS = {"Northwind Services": 150000, "Fabrikam Operations": 90000}


def stand_in(*, password: bool) -> EngineSpec:
    return EngineSpec(
        name=STAND_IN_ENGINE,
        label=LABEL,
        family="sqlalchemy",
        drivers={"sqlite+pysqlite": ("sqlalchemy", "sqlite3")},
        aliases={},
        extra=STAND_IN_ENGINE,
        query_parameters=frozenset(),
        tier="integration",
        password=password,
        schema=False,
        host_required=False,
        database_required=True,
    )


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """A SQLite file served through the server backend, relative to the cwd."""
    pytest.importorskip("sqlalchemy")
    monkeypatch.setitem(
        connections_module.ENGINES, STAND_IN_ENGINE, stand_in(password=False)
    )
    monkeypatch.setattr(connections_module, "_FILE_SCHEMES", {"duckdb": "DuckDB"})
    monkeypatch.chdir(tmp_path)
    yield make_database(tmp_path, "server.db")


def client_for(tmp_path: Path, chat: Any = None) -> DataIndexClient:
    return DataIndexClient(chat=chat, storage_path=tmp_path / ".dataindex")


def submit_server(client: DataIndexClient, **options: Any) -> dict[str, Any]:
    return client.submit_database(url=SERVER_URL, **database_arguments(), **options)


def query(client: DataIndexClient, doc_id: str) -> dict[str, Any]:
    return call_json(
        client,
        "query_database",
        {"doc_id": doc_id, "query_id": OVERDUE_QUERY_ID, "filters": OVERDUE_FILTERS},
    )


def stored_bytes(tmp_path: Path) -> bytes:
    return b"".join(
        path.read_bytes()
        for path in (tmp_path / ".dataindex").rglob("*")
        if path.is_file()
    )


# --- database servers --------------------------------------------------------------


def test_a_server_is_indexed_by_schema_and_answers_only_approved_queries(
    tmp_path: Path, server: Path
) -> None:
    connection_id = parse_connection({"url": SERVER_URL}).connection_id
    with client_for(tmp_path) as client:
        submitted = submit_server(client)
        doc_id = submitted["doc_id"]
        assert doc_id == f"db-{connection_id}"
        assert submitted["name"] == f"{LABEL} server.db"
        assert (submitted["status"], submitted["change"]) == ("completed", "new")
        assert submitted["source_type"] == SERVER_TYPE
        assert str(submitted["version"]).startswith(f"{STAND_IN_ENGINE}-schema:")

        info = client.get_document(doc_id)
        assert info["kind"] == "database"
        assert info["path"] is None
        assert info["connection"] == {"engine": STAND_IN_ENGINE, "url": SERVER_URL}
        assert "detail" not in info

        tree = call_json(client, "get_tree", {"doc_id": doc_id})
        titles = {node["title"] for node in tree["result"]}
        assert titles and "Northwind Services" not in str(tree)  # schema, not rows

        result = query(client, doc_id)
        assert rows_by_contract(result["value"]["rows"]) == overdue_totals(server)
        assert submit_server(client)["change"] == "unchanged"


def test_chat_cites_rows_that_the_server_calculated(
    tmp_path: Path, server: Path
) -> None:
    model = ScriptedModel(database_policy())
    with client_for(tmp_path, chat=model) as client:
        doc_id = submit_server(client)["doc_id"]
        answer = client.chat(DB_QUESTION, doc_id=doc_id)

    assert [claim.text for claim in answer.claims] == [DB_CLAIM]
    assert {citation.source_id for citation in answer.citations} == {doc_id}
    (record,) = (
        item for item in answer.evidence if item.operation == "databases.query"
    )
    assert rows_by_contract(record.value["rows"]) == EXPECTED_TOTALS
    prompts = " ".join(item.content for request in model.requests for item in request)
    assert SERVER_URL not in prompts


def test_a_schema_change_makes_the_server_stale_until_it_is_submitted_again(
    tmp_path: Path, server: Path
) -> None:
    with client_for(tmp_path) as client:
        doc_id = submit_server(client)["doc_id"]
        with closing(sqlite3.connect(server)) as connection:
            connection.execute("ALTER TABLE invoices ADD COLUMN note TEXT")
            connection.commit()

        info = client.get_document(doc_id)
        assert (info["status"], info["detail"]) == ("stale", SCHEMA_CHANGED)
        assert query(client, doc_id) == {"error": CHANGED}
        assert submit_server(client)["change"] == "updated"
        assert client.get_document(doc_id)["status"] == "completed"
        assert "error" not in query(client, doc_id)


def test_changing_the_approved_columns_of_a_server_updates_it(
    tmp_path: Path, server: Path
) -> None:
    arguments = database_arguments()
    narrower = {
        table: [column for column in columns if column != "reference"]
        for table, columns in arguments["allowed_columns"].items()
    }
    with client_for(tmp_path) as client:
        doc_id = submit_server(client)["doc_id"]
        assert "reference" in str(call_json(client, "get_tree", {"doc_id": doc_id}))
        resubmitted = client.submit_database(
            url=SERVER_URL, allowed_columns=narrower, queries=arguments["queries"]
        )
        assert (resubmitted["doc_id"], resubmitted["change"]) == (doc_id, "updated")
        assert "reference" not in str(call_json(client, "get_tree", {"doc_id": doc_id}))


def test_a_server_survives_a_restart_and_is_queried_without_resubmitting(
    tmp_path: Path, server: Path
) -> None:
    with client_for(tmp_path) as first:
        doc_id = submit_server(first, name="Billing")["doc_id"]
        before = first.list_documents()

    with client_for(tmp_path) as second:
        assert second.list_documents() == before
        assert second.get_document_id("Billing") == doc_id
        result = query(second, doc_id)
        assert rows_by_contract(result["value"]["rows"]) == EXPECTED_TOTALS


def test_an_unreachable_server_is_unavailable_with_a_fixed_detail(
    tmp_path: Path, server: Path
) -> None:
    with client_for(tmp_path) as client:
        doc_id = submit_server(client)["doc_id"]

    server.unlink()
    server.mkdir()  # SQLite cannot open a directory: the server is unreachable
    with client_for(tmp_path) as client:
        (listed,) = client.list_documents()["documents"]
        assert (listed["status"], listed["detail"]) == ("unavailable", UNREACHABLE)
        assert query(client, doc_id) == {"error": UNAVAILABLE}
        assert client.delete_document(doc_id)
        assert client.list_documents()["total"] == 0


def test_chat_about_an_unreachable_server_fails_before_calling_the_model(
    tmp_path: Path, server: Path
) -> None:
    with client_for(tmp_path) as client:
        submit_server(client, name="Billing")
    server.unlink()
    server.mkdir()

    model = ScriptedModel(database_policy())
    with (
        client_for(tmp_path, chat=model) as client,
        pytest.raises(ConfigurationError, match="could not be checked"),
    ):
        client.chat(DB_QUESTION, doc_id=client.get_document_id("Billing"))
    assert model.requests == []


def test_server_passwords_come_only_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: Path
) -> None:
    monkeypatch.setitem(
        connections_module.ENGINES, STAND_IN_ENGINE, stand_in(password=True)
    )
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    with client_for(tmp_path) as client:
        with pytest.raises(ValueError, match="must not contain a password"):
            client.submit_database(
                url=f"sqlite+pysqlite://reader:{SECRET}@/server.db",
                **database_arguments(),
            )
        with pytest.raises(SourceRejectedError) as unvalidated:
            client.submit_database(
                url=f"{SERVER_URL}?token={SECRET}", **database_arguments()
            )
        assert str(unvalidated.value).startswith(
            "the database connection was rejected: URL parameter 'token'"
        )
        assert SECRET not in str(unvalidated.value)
        with pytest.raises(SourceRejectedError, match=f"set the {PASSWORD_ENV}"):
            submit_server(client, password_env=PASSWORD_ENV)
        with pytest.raises(SourceRejectedError, match="password_env must name"):
            submit_server(client, password_env="HOME")

        # SQLite refuses any password, so this proves only that none leaks.
        monkeypatch.setenv(PASSWORD_ENV, SECRET)
        with pytest.raises(SourceRejectedError) as error:
            submit_server(client, password_env=PASSWORD_ENV)
        assert SECRET not in str(error.value)
        assert client.list_documents()["total"] == 0

    assert SECRET.encode() not in stored_bytes(tmp_path)


@pytest.mark.parametrize(
    ("arguments", "error", "message"),
    [
        ({}, ValueError, "exactly one of a database file path or a connection url"),
        (
            {"path": "server.db", "url": SERVER_URL},
            ValueError,
            "exactly one of a database file path or a connection url",
        ),
        (
            {"path": "server.db", "schema": "main"},
            ValueError,
            "only to a connection url",
        ),
        ({"url": "  "}, ValueError, "url must be a non-empty string"),
        ({"url": SERVER_URL, "schema": ""}, ValueError, "schema must be a non-empty"),
        (
            {"url": "duckdb:///contracts.duckdb"},
            SourceRejectedError,
            "submit the file by path",
        ),
        ({"url": "ftp://host/db"}, SourceRejectedError, "unsupported database URL"),
        (
            {"url": SERVER_URL, "schema": "main"},
            SourceRejectedError,
            "do not take a separate schema",
        ),
    ],
)
def test_bad_server_submissions_register_nothing(
    tmp_path: Path,
    server: Path,
    arguments: dict[str, Any],
    error: type[Exception],
    message: str,
) -> None:
    with client_for(tmp_path) as client:
        with pytest.raises(error, match=message):
            client.submit_database(**arguments, **database_arguments())
        assert client.list_documents()["total"] == 0


# --- DuckDB files ------------------------------------------------------------------


def make_duckdb(path: Path) -> Path:
    duckdb = pytest.importorskip("duckdb")
    script = "\n".join(
        line
        for line in DATABASE_FIXTURE.read_text(encoding="utf-8").splitlines()
        if not line.startswith("PRAGMA")
    )
    with closing(duckdb.connect(str(path))) as connection:
        connection.execute(script)
    return path


@pytest.mark.parametrize("filename", ["contracts.duckdb", "contracts.db"])
def test_a_duckdb_file_is_recognised_by_its_header_and_queried(
    tmp_path: Path, filename: str
) -> None:
    database = make_duckdb(tmp_path / filename)
    with client_for(tmp_path) as client:
        submitted = client.submit_database(database, **database_arguments())
        assert submitted["source_type"] == "application/vnd.duckdb"
        assert submitted["status"] == "completed"
        doc_id = str(submitted["doc_id"])
        result = query(client, doc_id)
        assert rows_by_contract(result["value"]["rows"]) == EXPECTED_TOTALS
        assert client.get_document(doc_id)["path"] == str(database.resolve())

    with client_for(tmp_path) as client:
        assert client.get_document(doc_id)["status"] == "completed"
        assert client.submit_database(database, **database_arguments())["change"] == (
            "unchanged"
        )
