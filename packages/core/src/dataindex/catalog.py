"""Versioned SQLite storage for source metadata, hierarchy, graph and evidence."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

from dataindex.exceptions import (
    CatalogCorruptionError,
    CatalogError,
    CatalogIntegrityError,
    CrossTenantRelationshipError,
    DuplicateNodeError,
    DuplicateSourceError,
    InvalidEvidenceError,
    NodeNotFoundError,
    SourceNotFoundError,
    StaleSourceVersionError,
    UnsupportedSchemaVersionError,
)
from dataindex.locators import LocatorDereferencer
from dataindex.models import (
    EvidenceKind,
    EvidenceRecord,
    NativeLocator,
    Node,
    Relationship,
    SourceRef,
)

SCHEMA_VERSION = 1
MAX_QUERY_LENGTH = 256
MAX_RESULT_LIMIT = 1000
MAX_SOURCE_NODE_LIMIT = 100_000
MAX_EVIDENCE_BYTES = 64 * 1024
MAX_TRAVERSAL_DEPTH = 16

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE sources (
        source_id TEXT PRIMARY KEY,
        source_type TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        current_version TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE source_revisions (
        source_id TEXT NOT NULL,
        version TEXT NOT NULL,
        recorded_at TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        ),
        PRIMARY KEY (source_id, version),
        FOREIGN KEY (source_id) REFERENCES sources(source_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE nodes (
        node_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        source_version TEXT NOT NULL,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        parent_id TEXT,
        locator_json TEXT NOT NULL,
        attributes_json TEXT NOT NULL,
        summary TEXT,
        UNIQUE (source_id, source_version, node_id),
        CHECK (parent_id IS NULL OR parent_id <> node_id),
        FOREIGN KEY (source_id, source_version)
            REFERENCES source_revisions(source_id, version) ON DELETE CASCADE,
        FOREIGN KEY (source_id, source_version, parent_id)
            REFERENCES nodes(source_id, source_version, node_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE evidence (
        evidence_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        source_version TEXT NOT NULL,
        node_id TEXT,
        locator_json TEXT NOT NULL,
        operation TEXT NOT NULL,
        value_json TEXT NOT NULL,
        evidence_kind TEXT NOT NULL,
        execution_id TEXT NOT NULL,
        unit TEXT,
        warnings_json TEXT NOT NULL,
        FOREIGN KEY (source_id, source_version)
            REFERENCES source_revisions(source_id, version) ON DELETE CASCADE,
        FOREIGN KEY (source_id, source_version, node_id)
            REFERENCES nodes(source_id, source_version, node_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE relationships (
        relationship_id TEXT PRIMARY KEY,
        subject_node_id TEXT NOT NULL,
        predicate TEXT NOT NULL,
        object_node_id TEXT NOT NULL,
        evidence_kind TEXT NOT NULL,
        identity_confidence REAL,
        CHECK (
            identity_confidence IS NULL OR
            (identity_confidence >= 0 AND identity_confidence <= 1)
        ),
        FOREIGN KEY (subject_node_id) REFERENCES nodes(node_id) ON DELETE CASCADE,
        FOREIGN KEY (object_node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE relationship_evidence (
        relationship_id TEXT NOT NULL,
        evidence_id TEXT NOT NULL,
        PRIMARY KEY (relationship_id, evidence_id),
        FOREIGN KEY (relationship_id)
            REFERENCES relationships(relationship_id) ON DELETE CASCADE,
        FOREIGN KEY (evidence_id) REFERENCES evidence(evidence_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX nodes_parent_idx ON nodes(source_id, source_version, parent_id)",
    "CREATE INDEX evidence_source_idx ON evidence(source_id, source_version)",
    "CREATE INDEX relationships_subject_idx ON relationships(subject_node_id)",
    "CREATE INDEX relationships_object_idx ON relationships(object_node_id)",
    "PRAGMA user_version = 1",
)


def _json_dumps(value: object) -> str:
    return json.dumps(
        _json_compatible(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _json_compatible(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _json_compatible(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_compatible(item) for item in value]
    return value


def _json_loads(value: str, field: str) -> object:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError) as error:
        raise CatalogCorruptionError(f"invalid JSON stored for {field}") from error


def _locator_json(locator: NativeLocator) -> str:
    return _json_dumps(
        {
            "scheme": locator.scheme,
            "locator_version": locator.locator_version,
            "value": locator.value,
        }
    )


def _decode_locator(value: str) -> NativeLocator:
    decoded = _json_loads(value, "native locator")
    if not isinstance(decoded, dict):
        raise CatalogCorruptionError("stored native locator must be an object")
    try:
        return NativeLocator(
            scheme=decoded["scheme"],
            locator_version=decoded["locator_version"],
            value=decoded["value"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise CatalogCorruptionError("stored native locator is malformed") from error


def _decode_node(row: sqlite3.Row) -> Node:
    attributes = _json_loads(row["attributes_json"], "node attributes")
    if not isinstance(attributes, dict):
        raise CatalogCorruptionError("stored node attributes must be an object")
    try:
        return Node(
            node_id=row["node_id"],
            source_id=row["source_id"],
            source_version=row["source_version"],
            kind=row["kind"],
            title=row["title"],
            parent_id=row["parent_id"],
            locator=_decode_locator(row["locator_json"]),
            attributes=attributes,
            summary=row["summary"],
        )
    except (TypeError, ValueError) as error:
        raise CatalogCorruptionError("stored node is malformed") from error


def _decode_evidence(row: sqlite3.Row) -> EvidenceRecord:
    value = _json_loads(row["value_json"], "evidence value")
    warnings = _json_loads(row["warnings_json"], "evidence warnings")
    if not isinstance(warnings, list) or not all(
        isinstance(warning, str) for warning in warnings
    ):
        raise CatalogCorruptionError("stored evidence warnings are malformed")
    try:
        return EvidenceRecord(
            source_id=row["source_id"],
            source_version=row["source_version"],
            node_id=row["node_id"],
            locator=_decode_locator(row["locator_json"]),
            operation=row["operation"],
            value=value,
            evidence_kind=EvidenceKind(row["evidence_kind"]),
            execution_id=row["execution_id"],
            unit=row["unit"],
            warnings=tuple(warnings),
        )
    except (TypeError, ValueError) as error:
        raise CatalogCorruptionError("stored evidence is malformed") from error


def _unreadable_catalog(error: sqlite3.Error) -> CatalogError:
    return CatalogError(
        f"could not read the catalog ({type(error).__name__}); "
        "the file may be corrupt or not a DataIndex catalog"
    )


class SQLiteCatalog:
    """A local, tenant-scoped metadata catalog backed by SQLite.

    Source content stays outside the catalog. A source revision change preserves
    revision metadata while removing nodes, edges and evidence from the old
    active snapshot.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        database = str(path)
        try:
            self._connection = sqlite3.connect(database, isolation_level=None)
        except sqlite3.Error as error:
            raise _unreadable_catalog(error) from error
        self._connection.row_factory = sqlite3.Row
        self._closed = False
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._migrate()
        except sqlite3.Error as error:
            self.close()
            raise _unreadable_catalog(error) from error
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> SQLiteCatalog:
        self._ensure_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    @property
    def schema_version(self) -> int:
        self._ensure_open()
        return int(self._connection.execute("PRAGMA user_version").fetchone()[0])

    def _ensure_open(self) -> None:
        if self._closed:
            raise CatalogIntegrityError("catalog is closed")

    @contextmanager
    def _transaction(self) -> Generator[None, None, None]:
        self._ensure_open()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _migrate(self) -> None:
        version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(
                f"catalog schema {version} is newer than supported schema "
                f"{SCHEMA_VERSION}"
            )
        if version == SCHEMA_VERSION:
            return
        if version != 0:
            raise UnsupportedSchemaVersionError(
                f"no migration path from catalog schema {version}"
            )
        with self._transaction():
            for statement in _SCHEMA_STATEMENTS:
                self._connection.execute(statement)

    @staticmethod
    def _validate_tenant_id(tenant_id: str) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_RESULT_LIMIT
        ):
            raise ValueError(f"limit must be between 1 and {MAX_RESULT_LIMIT}")

    def create_source(
        self, source: SourceRef, *, tenant_id: str = "local"
    ) -> None:
        self._ensure_open()
        self._validate_tenant_id(tenant_id)
        with self._transaction():
            try:
                self._connection.execute(
                    "INSERT INTO sources(source_id, source_type, tenant_id, "
                    "current_version) VALUES (?, ?, ?, ?)",
                    (source.source_id, source.source_type, tenant_id, source.version),
                )
                self._connection.execute(
                    "INSERT INTO source_revisions(source_id, version) VALUES (?, ?)",
                    (source.source_id, source.version),
                )
            except sqlite3.IntegrityError as error:
                if self._source_row(source.source_id) is not None:
                    raise DuplicateSourceError(
                        f"source {source.source_id!r} already exists"
                    ) from error
                raise CatalogIntegrityError("source could not be created") from error

    def update_source(self, source: SourceRef) -> None:
        """Switch a source to a revision, invalidating its previous snapshot."""
        self._ensure_open()
        with self._transaction():
            existing = self._source_row(source.source_id)
            if existing is None:
                raise SourceNotFoundError(f"source {source.source_id!r} is not indexed")
            if existing["source_type"] != source.source_type:
                raise CatalogIntegrityError("a source's type cannot be changed")
            if existing["current_version"] == source.version:
                return
            self._connection.execute(
                "DELETE FROM nodes WHERE source_id = ?", (source.source_id,)
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO source_revisions(source_id, version) "
                "VALUES (?, ?)",
                (source.source_id, source.version),
            )
            self._connection.execute(
                "UPDATE sources SET current_version = ? WHERE source_id = ?",
                (source.version, source.source_id),
            )

    def remove_source(self, source_id: str) -> None:
        self._ensure_open()
        with self._transaction():
            cursor = self._connection.execute(
                "DELETE FROM sources WHERE source_id = ?", (source_id,)
            )
            if cursor.rowcount == 0:
                raise SourceNotFoundError(f"source {source_id!r} is not indexed")

    def get_source(self, source_id: str) -> SourceRef | None:
        self._ensure_open()
        row = self._source_row(source_id)
        if row is None:
            return None
        return SourceRef(
            source_id=row["source_id"],
            source_type=row["source_type"],
            version=row["current_version"],
        )

    def get_source_tenant(self, source_id: str) -> str | None:
        self._ensure_open()
        row = self._source_row(source_id)
        return None if row is None else str(row["tenant_id"])

    def list_sources(
        self, tenant_id: str, *, limit: int = 100
    ) -> tuple[SourceRef, ...]:
        self._ensure_open()
        self._validate_tenant_id(tenant_id)
        self._validate_limit(limit)
        rows = self._connection.execute(
            "SELECT source_id, source_type, current_version FROM sources "
            "WHERE tenant_id = ? ORDER BY source_id LIMIT ?",
            (tenant_id, limit),
        )
        return tuple(
            SourceRef(
                source_id=row["source_id"],
                source_type=row["source_type"],
                version=row["current_version"],
            )
            for row in rows
        )

    def list_source_revisions(self, source_id: str) -> tuple[str, ...]:
        self._ensure_open()
        if self._source_row(source_id) is None:
            raise SourceNotFoundError(f"source {source_id!r} is not indexed")
        rows = self._connection.execute(
            "SELECT version FROM source_revisions WHERE source_id = ? "
            "ORDER BY recorded_at, version",
            (source_id,),
        )
        return tuple(row["version"] for row in rows)

    def _source_row(self, source_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT source_id, source_type, tenant_id, current_version "
            "FROM sources WHERE source_id = ?",
            (source_id,),
        ).fetchone()

    def _require_current_source(self, source_id: str, version: str) -> sqlite3.Row:
        row = self._source_row(source_id)
        if row is None:
            raise SourceNotFoundError(f"source {source_id!r} is not indexed")
        if row["current_version"] != version:
            raise StaleSourceVersionError(
                f"source {source_id!r} is at {row['current_version']!r}, "
                f"not {version!r}"
            )
        return row

    def create_node(self, node: Node) -> None:
        self._ensure_open()
        with self._transaction():
            self._create_node(node)

    def update_node(self, node: Node) -> None:
        """Update a node without allowing it to move between source revisions."""
        self._ensure_open()
        with self._transaction():
            self._update_node(node)

    def upsert_nodes(self, nodes: Sequence[Node]) -> None:
        """Create or update a batch of nodes in one transaction."""
        self._ensure_open()
        seen_ids: set[str] = set()
        with self._transaction():
            for node in nodes:
                if node.node_id in seen_ids:
                    raise DuplicateNodeError(
                        f"node {node.node_id!r} appears more than once in the batch"
                    )
                seen_ids.add(node.node_id)
                existing = self._connection.execute(
                    "SELECT source_id, source_version FROM nodes WHERE node_id = ?",
                    (node.node_id,),
                ).fetchone()
                if existing is None:
                    self._create_node(node)
                elif (
                    existing["source_id"] != node.source_id
                    or existing["source_version"] != node.source_version
                ):
                    raise CatalogIntegrityError(
                        "a node cannot move between sources or source revisions"
                    )
                elif self.get_node(node.node_id) != node:
                    self._update_node(node)

    def _create_node(self, node: Node) -> None:
        self._require_current_source(node.source_id, node.source_version)
        if self._connection.execute(
            "SELECT 1 FROM nodes WHERE node_id = ?", (node.node_id,)
        ).fetchone():
            raise DuplicateNodeError(f"node {node.node_id!r} already exists")
        self._validate_parent(node)
        try:
            self._insert_node(node)
        except sqlite3.IntegrityError as error:
            raise CatalogIntegrityError(
                f"node {node.node_id!r} violates catalog constraints"
            ) from error

    def _update_node(self, node: Node) -> None:
        existing = self._connection.execute(
            "SELECT source_id, source_version FROM nodes WHERE node_id = ?",
            (node.node_id,),
        ).fetchone()
        if existing is None:
            raise NodeNotFoundError(f"node {node.node_id!r} is not indexed")
        if (
            existing["source_id"] != node.source_id
            or existing["source_version"] != node.source_version
        ):
            raise CatalogIntegrityError(
                "a node cannot move between sources or source revisions"
            )
        self._require_current_source(node.source_id, node.source_version)
        self._validate_parent(node)
        self._invalidate_node_evidence(node.node_id)
        try:
            self._connection.execute(
                "UPDATE nodes SET kind = ?, title = ?, parent_id = ?, "
                "locator_json = ?, attributes_json = ?, summary = ? "
                "WHERE node_id = ?",
                (
                    node.kind,
                    node.title,
                    node.parent_id,
                    _locator_json(node.locator),
                    _json_dumps(node.attributes),
                    node.summary,
                    node.node_id,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise CatalogIntegrityError(
                f"node {node.node_id!r} violates catalog constraints"
            ) from error

    def remove_node(self, node_id: str) -> None:
        self._ensure_open()
        with self._transaction():
            if self._connection.execute(
                "SELECT 1 FROM nodes WHERE node_id = ?", (node_id,)
            ).fetchone() is None:
                raise NodeNotFoundError(f"node {node_id!r} is not indexed")
            if self._connection.execute(
                "SELECT 1 FROM nodes WHERE parent_id = ? LIMIT 1", (node_id,)
            ).fetchone():
                raise CatalogIntegrityError("cannot remove a node that has children")
            self._invalidate_node_evidence(node_id)
            self._connection.execute(
                "DELETE FROM relationships WHERE subject_node_id = ? "
                "OR object_node_id = ?",
                (node_id, node_id),
            )
            self._connection.execute("DELETE FROM nodes WHERE node_id = ?", (node_id,))

    def _insert_node(self, node: Node) -> None:
        self._connection.execute(
            "INSERT INTO nodes(node_id, source_id, source_version, kind, title, "
            "parent_id, locator_json, attributes_json, summary) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                node.node_id,
                node.source_id,
                node.source_version,
                node.kind,
                node.title,
                node.parent_id,
                _locator_json(node.locator),
                _json_dumps(node.attributes),
                node.summary,
            ),
        )

    def _validate_parent(self, node: Node) -> None:
        if node.parent_id is None:
            return
        parent = self.get_node(node.parent_id)
        if parent is None:
            raise NodeNotFoundError(f"parent node {node.parent_id!r} is not indexed")
        if (
            parent.source_id != node.source_id
            or parent.source_version != node.source_version
        ):
            raise CatalogIntegrityError(
                "a node's parent must belong to the same source revision"
            )
        ancestor_id: str | None = parent.node_id
        while ancestor_id is not None:
            if ancestor_id == node.node_id:
                raise CatalogIntegrityError("node parent assignment would create a cycle")
            ancestor = self.get_node(ancestor_id)
            ancestor_id = ancestor.parent_id if ancestor is not None else None

    def _invalidate_node_evidence(self, node_id: str) -> None:
        self._connection.execute(
            "DELETE FROM relationships WHERE relationship_id IN ("
            "SELECT re.relationship_id FROM relationship_evidence AS re "
            "JOIN evidence AS e ON e.evidence_id = re.evidence_id "
            "WHERE e.node_id = ?)",
            (node_id,),
        )
        self._connection.execute("DELETE FROM evidence WHERE node_id = ?", (node_id,))

    def get_node(self, node_id: str) -> Node | None:
        self._ensure_open()
        row = self._connection.execute(
            "SELECT n.* FROM nodes AS n JOIN sources AS s "
            "ON s.source_id = n.source_id AND s.current_version = n.source_version "
            "WHERE n.node_id = ?",
            (node_id,),
        ).fetchone()
        return _decode_node(row) if row is not None else None

    def list_children(
        self,
        parent_id: str | None = None,
        *,
        source_id: str | None = None,
        limit: int = 100,
    ) -> tuple[Node, ...]:
        self._ensure_open()
        self._validate_limit(limit)
        clauses = ["s.current_version = n.source_version"]
        parameters: list[object] = []
        if parent_id is None:
            clauses.append("n.parent_id IS NULL")
        else:
            clauses.append("n.parent_id = ?")
            parameters.append(parent_id)
        if source_id is not None:
            clauses.append("n.source_id = ?")
            parameters.append(source_id)
        parameters.append(limit)
        rows = self._connection.execute(
            "SELECT n.* FROM nodes AS n JOIN sources AS s "
            "ON s.source_id = n.source_id WHERE "
            + " AND ".join(clauses)
            + " ORDER BY n.source_id, n.title, n.node_id LIMIT ?",
            parameters,
        )
        return tuple(_decode_node(row) for row in rows)

    def list_source_nodes(
        self, source_id: str, *, limit: int = MAX_RESULT_LIMIT
    ) -> tuple[Node, ...]:
        """Return up to ``limit`` nodes of the current revision in insertion order.

        One statement is used, so large documents can be outlined without a
        query per node. Parents are always inserted before their children.
        """
        self._ensure_open()
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_SOURCE_NODE_LIMIT
        ):
            raise ValueError(f"limit must be between 1 and {MAX_SOURCE_NODE_LIMIT}")
        rows = self._connection.execute(
            "SELECT n.* FROM nodes AS n JOIN sources AS s "
            "ON s.source_id = n.source_id AND s.current_version = n.source_version "
            "WHERE n.source_id = ? ORDER BY n.rowid LIMIT ?",
            (source_id, limit),
        )
        return tuple(_decode_node(row) for row in rows)

    def search_nodes(
        self,
        query: str,
        *,
        source_id: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> tuple[Node, ...]:
        self._ensure_open()
        self._validate_limit(limit)
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if len(query) > MAX_QUERY_LENGTH:
            raise ValueError(f"query must be at most {MAX_QUERY_LENGTH} characters")
        clauses = [
            "s.current_version = n.source_version",
            "(instr(lower(n.title), lower(?)) > 0 OR "
            "instr(lower(coalesce(n.summary, '')), lower(?)) > 0)",
        ]
        parameters: list[object] = [query.strip(), query.strip()]
        if source_id is not None:
            clauses.append("n.source_id = ?")
            parameters.append(source_id)
        if kind is not None:
            clauses.append("n.kind = ?")
            parameters.append(kind)
        parameters.append(limit)
        rows = self._connection.execute(
            "SELECT n.* FROM nodes AS n JOIN sources AS s "
            "ON s.source_id = n.source_id WHERE "
            + " AND ".join(clauses)
            + " ORDER BY n.source_id, n.title, n.node_id LIMIT ?",
            parameters,
        )
        return tuple(_decode_node(row) for row in rows)

    def verify_evidence(
        self,
        evidence: EvidenceRecord,
        dereferencer: LocatorDereferencer,
    ) -> None:
        """Validate source revision and resolve its locator against native data."""
        self._ensure_open()
        source_row = self._require_current_source(
            evidence.source_id, evidence.source_version
        )
        if evidence.node_id is not None:
            node = self.get_node(evidence.node_id)
            if node is None:
                raise InvalidEvidenceError(
                    f"evidence node {evidence.node_id!r} is not indexed"
                )
            if (
                node.source_id != evidence.source_id
                or node.source_version != evidence.source_version
            ):
                raise InvalidEvidenceError(
                    "evidence node and source revision do not match"
                )
            if node.locator != evidence.locator:
                raise InvalidEvidenceError(
                    "evidence locator does not match the indexed node locator"
                )
        source = SourceRef(
            source_id=source_row["source_id"],
            source_type=source_row["source_type"],
            version=source_row["current_version"],
        )
        try:
            resolved_value = dereferencer.dereference(source, evidence.locator)
        except LookupError as error:
            raise InvalidEvidenceError(
                "native locator does not resolve in the authoritative source"
            ) from error
        if evidence.evidence_kind is EvidenceKind.OBSERVED:
            try:
                matches = _json_dumps(resolved_value) == _json_dumps(evidence.value)
            except (TypeError, ValueError) as error:
                raise InvalidEvidenceError(
                    "dereferenced evidence value is not JSON-compatible"
                ) from error
            if not matches:
                raise InvalidEvidenceError(
                    "observed evidence value does not match the native source"
                )

    def create_evidence(
        self,
        evidence: EvidenceRecord,
        dereferencer: LocatorDereferencer,
    ) -> str:
        """Verify and persist bounded provenance; return its opaque evidence ID."""
        serialized_value = _json_dumps(evidence.value)
        if len(serialized_value.encode("utf-8")) > MAX_EVIDENCE_BYTES:
            raise InvalidEvidenceError(
                f"evidence value exceeds the {MAX_EVIDENCE_BYTES}-byte limit"
            )
        serialized_locator = _locator_json(evidence.locator)
        self.verify_evidence(evidence, dereferencer)
        evidence_id = str(uuid.uuid4())
        with self._transaction():
            self._require_current_source(evidence.source_id, evidence.source_version)
            if evidence.node_id is not None:
                current_node = self.get_node(evidence.node_id)
                if current_node is None:
                    raise InvalidEvidenceError(
                        f"evidence node {evidence.node_id!r} is no longer indexed"
                    )
                if (
                    current_node.source_id != evidence.source_id
                    or current_node.source_version != evidence.source_version
                    or current_node.locator != evidence.locator
                ):
                    raise InvalidEvidenceError(
                        "evidence node changed after locator verification"
                    )
            try:
                self._connection.execute(
                    "INSERT INTO evidence(evidence_id, source_id, source_version, "
                    "node_id, locator_json, operation, value_json, evidence_kind, "
                    "execution_id, unit, warnings_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        evidence_id,
                        evidence.source_id,
                        evidence.source_version,
                        evidence.node_id,
                        serialized_locator,
                        evidence.operation,
                        serialized_value,
                        evidence.evidence_kind.value,
                        evidence.execution_id,
                        evidence.unit,
                        _json_dumps(list(evidence.warnings)),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise InvalidEvidenceError(
                    "evidence violates source or node constraints"
                ) from error
        return evidence_id

    def get_evidence(self, evidence_id: str) -> EvidenceRecord | None:
        self._ensure_open()
        row = self._connection.execute(
            "SELECT e.* FROM evidence AS e JOIN sources AS s "
            "ON s.source_id = e.source_id AND s.current_version = e.source_version "
            "WHERE e.evidence_id = ?",
            (evidence_id,),
        ).fetchone()
        return _decode_evidence(row) if row is not None else None

    def create_relationship(self, relationship: Relationship) -> None:
        self._ensure_open()
        if len(set(relationship.evidence_ids)) != len(relationship.evidence_ids):
            raise CatalogIntegrityError("relationship evidence IDs must be unique")
        with self._transaction():
            subject = self._current_node_row(relationship.subject_node_id)
            object_ = self._current_node_row(relationship.object_node_id)
            if subject is None:
                raise NodeNotFoundError(
                    f"subject node {relationship.subject_node_id!r} is not indexed"
                )
            if object_ is None:
                raise NodeNotFoundError(
                    f"object node {relationship.object_node_id!r} is not indexed"
                )
            if subject["tenant_id"] != object_["tenant_id"]:
                raise CrossTenantRelationshipError(
                    "relationships cannot link nodes across tenants"
                )
            placeholders = ",".join("?" for _ in relationship.evidence_ids)
            rows = self._connection.execute(
                "SELECT e.evidence_id, e.source_id, e.evidence_kind, s.tenant_id "
                "FROM evidence AS e JOIN sources AS s ON s.source_id = e.source_id "
                "AND s.current_version = e.source_version WHERE e.evidence_id IN ("
                + placeholders
                + ")",
                relationship.evidence_ids,
            ).fetchall()
            evidence_by_id = {row["evidence_id"]: row for row in rows}
            if len(evidence_by_id) != len(relationship.evidence_ids):
                raise InvalidEvidenceError(
                    "relationship references missing or stale evidence"
                )
            endpoint_sources = {subject["source_id"], object_["source_id"]}
            for evidence_id in relationship.evidence_ids:
                row = evidence_by_id[evidence_id]
                if row["tenant_id"] != subject["tenant_id"]:
                    raise CrossTenantRelationshipError(
                        "relationship evidence belongs to another tenant"
                    )
                if row["source_id"] not in endpoint_sources:
                    raise InvalidEvidenceError(
                        "relationship evidence must cite one of its endpoint sources"
                    )
                if row["evidence_kind"] != relationship.evidence_kind.value:
                    raise InvalidEvidenceError(
                        "relationship evidence kind must match the relationship kind"
                    )
            try:
                self._connection.execute(
                    "INSERT INTO relationships(relationship_id, subject_node_id, "
                    "predicate, object_node_id, evidence_kind, identity_confidence) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        relationship.relationship_id,
                        relationship.subject_node_id,
                        relationship.predicate,
                        relationship.object_node_id,
                        relationship.evidence_kind.value,
                        relationship.identity_confidence,
                    ),
                )
                self._connection.executemany(
                    "INSERT INTO relationship_evidence(relationship_id, evidence_id) "
                    "VALUES (?, ?)",
                    (
                        (relationship.relationship_id, evidence_id)
                        for evidence_id in sorted(relationship.evidence_ids)
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise CatalogIntegrityError(
                    f"relationship {relationship.relationship_id!r} "
                    "violates catalog constraints"
                ) from error

    def get_relationship(self, relationship_id: str) -> Relationship | None:
        self._ensure_open()
        row = self._connection.execute(
            "SELECT * FROM relationships WHERE relationship_id = ?",
            (relationship_id,),
        ).fetchone()
        return self._decode_relationship(row) if row is not None else None

    def _decode_relationship(self, row: sqlite3.Row) -> Relationship:
        evidence_rows = self._connection.execute(
            "SELECT evidence_id FROM relationship_evidence "
            "WHERE relationship_id = ? ORDER BY evidence_id",
            (row["relationship_id"],),
        )
        evidence_ids = tuple(item["evidence_id"] for item in evidence_rows)
        try:
            return Relationship(
                relationship_id=row["relationship_id"],
                subject_node_id=row["subject_node_id"],
                predicate=row["predicate"],
                object_node_id=row["object_node_id"],
                evidence_ids=evidence_ids,
                evidence_kind=EvidenceKind(row["evidence_kind"]),
                identity_confidence=row["identity_confidence"],
            )
        except (TypeError, ValueError) as error:
            raise CatalogCorruptionError("stored relationship is malformed") from error

    def _current_node_row(self, node_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT n.node_id, n.source_id, s.tenant_id FROM nodes AS n "
            "JOIN sources AS s ON s.source_id = n.source_id "
            "AND s.current_version = n.source_version WHERE n.node_id = ?",
            (node_id,),
        ).fetchone()

    def traverse_relationships(
        self,
        start_node_id: str,
        *,
        direction: str = "outgoing",
        predicate: str | None = None,
        max_depth: int = 3,
        limit: int = 100,
    ) -> tuple[Relationship, ...]:
        """Walk current graph edges breadth-first with explicit depth/result bounds."""
        self._ensure_open()
        self._validate_limit(limit)
        if direction not in {"outgoing", "incoming", "both"}:
            raise ValueError("direction must be 'outgoing', 'incoming' or 'both'")
        if (
            isinstance(max_depth, bool)
            or not isinstance(max_depth, int)
            or not 1 <= max_depth <= MAX_TRAVERSAL_DEPTH
        ):
            raise ValueError(
                f"max_depth must be between 1 and {MAX_TRAVERSAL_DEPTH}"
            )
        if self._current_node_row(start_node_id) is None:
            raise NodeNotFoundError(f"node {start_node_id!r} is not indexed")
        frontier = [start_node_id]
        visited_nodes = {start_node_id}
        visited_relationships: set[str] = set()
        result: list[Relationship] = []
        for _ in range(max_depth):
            next_frontier: set[str] = set()
            for node_id in frontier:
                if direction == "outgoing":
                    clause = "r.subject_node_id = ?"
                elif direction == "incoming":
                    clause = "r.object_node_id = ?"
                else:
                    clause = "(r.subject_node_id = ? OR r.object_node_id = ?)"
                parameters: list[object] = [node_id]
                if direction == "both":
                    parameters.append(node_id)
                predicate_clause = ""
                if predicate is not None:
                    predicate_clause = " AND r.predicate = ?"
                    parameters.append(predicate)
                rows = self._connection.execute(
                    "SELECT r.* FROM relationships AS r "
                    "JOIN nodes AS subject ON subject.node_id = r.subject_node_id "
                    "JOIN sources AS ss ON ss.source_id = subject.source_id "
                    "AND ss.current_version = subject.source_version "
                    "JOIN nodes AS object ON object.node_id = r.object_node_id "
                    "JOIN sources AS os ON os.source_id = object.source_id "
                    "AND os.current_version = object.source_version "
                    "WHERE "
                    + clause
                    + predicate_clause
                    + " ORDER BY r.relationship_id",
                    parameters,
                )
                for row in rows:
                    relationship_id = row["relationship_id"]
                    if relationship_id in visited_relationships:
                        continue
                    visited_relationships.add(relationship_id)
                    result.append(self._decode_relationship(row))
                    if len(result) >= limit:
                        return tuple(result)
                    if direction == "outgoing":
                        neighbor = row["object_node_id"]
                    elif direction == "incoming":
                        neighbor = row["subject_node_id"]
                    elif row["subject_node_id"] == node_id:
                        neighbor = row["object_node_id"]
                    else:
                        neighbor = row["subject_node_id"]
                    if neighbor not in visited_nodes:
                        visited_nodes.add(neighbor)
                        next_frontier.add(neighbor)
            frontier = sorted(next_frontier)
            if not frontier:
                break
        return tuple(result)

