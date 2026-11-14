"""The sample ``dataindex submit-db --config`` file stays valid and in sync."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from dataindex import SourceRef
from dataindex.database_config import normalize_database_config
from dataindex_ext_databases import SQLiteDatabaseExtension
from examples.mixed_sources import DATABASE_FIXTURE, _query_definitions

CONFIG_PATH = (
    Path(__file__).resolve().parents[2] / "examples" / "contracts_database_config.json"
)
SOURCE = SourceRef(
    "sample-contracts", "application/vnd.sqlite3", "sqlite-live:sample-v1"
)


@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_sample_config_matches_the_example_query_definitions(
    config: dict[str, Any],
) -> None:
    assert set(config) == {"allowed_columns", "queries"}
    assert config["queries"] == [query.to_mapping() for query in _query_definitions()]


def test_sample_config_is_accepted_by_core_without_changes(
    config: dict[str, Any],
) -> None:
    normalized = normalize_database_config(**config)

    assert normalized["queries"] == config["queries"]
    assert set(normalized["allowed_columns"]) == {"contracts", "invoices"}


def test_sample_config_registers_against_the_fixture_database(
    config: dict[str, Any], tmp_path: Path
) -> None:
    database = tmp_path / "contracts.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(DATABASE_FIXTURE.read_text(encoding="utf-8"))
    finally:
        connection.close()
    extension = SQLiteDatabaseExtension()
    try:
        extension.register_database_config(SOURCE, database, config)

        (tool,) = extension.tools_for_source(SOURCE)

        assert tool.input_schema["properties"]["query_id"]["enum"] == [
            "invoices.overdue_total_by_contract"
        ]
    finally:
        extension.close()
