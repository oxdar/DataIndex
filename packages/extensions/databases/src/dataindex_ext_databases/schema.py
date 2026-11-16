"""Engine-neutral description of an allowlisted database schema."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping

MAX_SCHEMA_TABLES = 256
MAX_SCHEMA_COLUMNS = 4096
MAX_COLUMNS_PER_TABLE = 512
MAX_SCHEMA_FOREIGN_KEYS = 4096
MAX_DECLARED_TYPE_CHARS = 128


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    name: str
    declared_type: str
    not_null: bool
    primary_key_position: int
    ordinal: int


@dataclass(frozen=True, slots=True)
class ForeignKeyInfo:
    constraint_id: int
    sequence: int
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    on_update: str
    on_delete: str


@dataclass(frozen=True, slots=True)
class TableInfo:
    name: str
    columns: tuple[ColumnInfo, ...]
    foreign_keys: tuple[ForeignKeyInfo, ...]
    primary_key_columns: tuple[str, ...]
    object_kind: str = "table"


@dataclass(frozen=True, slots=True)
class SchemaInfo:
    """``version`` is SQLite's schema counter or a digest of reflected metadata."""

    version: int | str
    tables: Mapping[str, TableInfo]
