from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pytest

from dataindex import (
    EntitlementDecision,
    EntitlementDeniedError,
    EntitlementReason,
    IndexContext,
    PlannedToolCall,
    SourceAuthorizationError,
    SourceRef,
    SQLiteCatalog,
    ToolDispatcher,
    ToolRequest,
)
from dataindex.registry import ExtensionRegistry
from dataindex_ext_databases import (
    ColumnRef,
    FixedFilterRule,
    QueryDefinition,
    SQLiteDatabaseExtension,
)
from dataindex_ext_documents.plugin import DocumentIndexExtension

PLUGIN_ID = "premium-demo"
DISTRIBUTION = "dataindex-ext-mock-premium"
PLUGIN_VERSION = "0.1.0"
SOURCE_TYPE = "application/vnd.dataindex.mock-premium"
TOOL_ID = "premium.demo_lookup"
ENTITLEMENT_ID = "premium.demo.read"
DEPLOYMENT = "test-host"
PREMIUM_SOURCE = SourceRef("premium-fixture", SOURCE_TYPE, "synthetic-r1")
DOCUMENT_SOURCE = SourceRef("free-fixture", "text/markdown", "fixture-r1")
DATABASE_SOURCE = SourceRef(
    "free-sql-fixture", "application/vnd.sqlite3", "sqlite-live:fixture-r1"
)
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class EntitlementAuditEvent:
    tenant_id: str
    capability: str
    deployment: str
    decision: EntitlementDecision
    checked_at: datetime


@dataclass
class _Grant:
    expires_at: datetime | None
    revoked: bool = False


class InMemoryEntitlementProvider:
    """Test-only provider with explicit, inspectable decision records."""

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._clock = clock
        self._grants: dict[tuple[str, str, str], _Grant] = {}
        self._audit: list[EntitlementAuditEvent] = []

    @property
    def audit_events(self) -> tuple[EntitlementAuditEvent, ...]:
        return tuple(self._audit)

    def grant(
        self,
        tenant_id: str,
        capability: str,
        deployment: str,
        *,
        expires_at: datetime | None = None,
    ) -> None:
        self._grants[(tenant_id, capability, deployment)] = _Grant(expires_at)

    def revoke(self, tenant_id: str, capability: str, deployment: str) -> None:
        grant = self._grants.get((tenant_id, capability, deployment))
        if grant is not None:
            grant.revoked = True

    def check(
        self, tenant_id: str, capability: str, deployment: str
    ) -> EntitlementDecision:
        now = self._clock()
        grant = self._grants.get((tenant_id, capability, deployment))
        if grant is None:
            other_tenant = any(
                key[1:] == (capability, deployment) for key in self._grants
            )
            other_deployment = any(
                key[:2] == (tenant_id, capability) for key in self._grants
            )
            reason = (
                EntitlementReason.TENANT_MISMATCH
                if other_tenant
                else EntitlementReason.DEPLOYMENT_MISMATCH
                if other_deployment
                else EntitlementReason.NOT_FOUND
            )
            decision = EntitlementDecision(False, reason)
        elif grant.revoked:
            decision = EntitlementDecision(False, EntitlementReason.REVOKED)
        elif grant.expires_at is not None and grant.expires_at <= now:
            decision = EntitlementDecision(False, EntitlementReason.EXPIRED)
        else:
            decision = EntitlementDecision(True, EntitlementReason.GRANTED)
        self._audit.append(
            EntitlementAuditEvent(
                tenant_id, capability, deployment, decision, now
            )
        )
        return decision


class SourceAllowlist:
    def is_authorized(
        self, tenant_id: str, source: SourceRef, capability: str
    ) -> bool:
        return (
            tenant_id == "tenant-a"
            and source.source_id
            in {
                PREMIUM_SOURCE.source_id,
                DOCUMENT_SOURCE.source_id,
                DATABASE_SOURCE.source_id,
            }
            and capability
            in {
                "source.access",
                ENTITLEMENT_ID,
                "document.retrieve",
                "database.query",
            }
        )


@dataclass
class ContractEnvironment:
    catalog: SQLiteCatalog
    dispatcher: ToolDispatcher
    registry: ExtensionRegistry

    def close(self) -> None:
        for extension in self.registry.loaded_extensions.values():
            extension.close()
        self.catalog.close()


def _environment(
    provider: InMemoryEntitlementProvider | None,
    temporary_directory: Path,
) -> ContractEnvironment:
    registry = ExtensionRegistry(
        trusted_distributions={
            (DISTRIBUTION, PLUGIN_VERSION),
            ("dataindex-ext-documents", "0.1.0"),
            ("dataindex-ext-databases", "0.1.0"),
        },
        enabled_plugin_ids={PLUGIN_ID, "documents", "databases"},
    )
    statuses = registry.discover()
    premium_status = next(status for status in statuses if status.plugin_id == PLUGIN_ID)
    assert premium_status.installed and premium_status.discovered
    assert premium_status.compatible and premium_status.trusted and premium_status.enabled
    assert PLUGIN_ID not in registry.loaded_extensions
    registry.load_enabled()
    assert PLUGIN_ID in registry.loaded_extensions
    assert "documents" in registry.loaded_extensions
    assert "databases" in registry.loaded_extensions

    catalog = SQLiteCatalog()
    catalog.create_source(PREMIUM_SOURCE, tenant_id="tenant-a")
    catalog.create_source(DOCUMENT_SOURCE, tenant_id="tenant-a")
    catalog.create_source(DATABASE_SOURCE, tenant_id="tenant-a")
    document_extension = registry.loaded_extensions["documents"]
    assert isinstance(document_extension, DocumentIndexExtension)
    document_extension.register_document(
        DOCUMENT_SOURCE, "# Free terms\n\nPayment is due in 30 days.\n"
    )
    for node in document_extension.iter_index_records(DOCUMENT_SOURCE, IndexContext()):
        catalog.create_node(node)
    database_extension = registry.loaded_extensions["databases"]
    assert isinstance(database_extension, SQLiteDatabaseExtension)
    database_path = temporary_directory / "facts.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE facts (id INTEGER PRIMARY KEY, tenant_id TEXT NOT NULL, "
            "label TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO facts (id, tenant_id, label) VALUES (?, ?, ?)",
            (1, "tenant-a", "Synthetic SQL fact"),
        )
        connection.execute(
            "INSERT INTO facts (id, tenant_id, label) VALUES (?, ?, ?)",
            (2, "tenant-b", "Other tenant fact"),
        )
    database_extension.register_database(
        DATABASE_SOURCE,
        database_path,
        allowed_columns={"facts": ("id", "tenant_id", "label")},
        queries=(
            QueryDefinition(
                query_id="facts.list",
                table="facts",
                columns=(ColumnRef("facts", "id"), ColumnRef("facts", "label")),
                source_filters=(
                    FixedFilterRule(
                        "tenant_scope",
                        ColumnRef("facts", "tenant_id"),
                        "eq",
                        "tenant-a",
                    ),
                ),
                order_by=(ColumnRef("facts", "id"),),
                max_rows=1,
            ),
        ),
    )
    for node in database_extension.iter_index_records(DATABASE_SOURCE, IndexContext()):
        catalog.create_node(node)
    return ContractEnvironment(
        catalog,
        ToolDispatcher(
            registry,
            catalog,
            SourceAllowlist(),
            provider,
            deployment=DEPLOYMENT,
        ),
        registry,
    )


def _premium_call() -> PlannedToolCall:
    return PlannedToolCall(
        PLUGIN_ID,
        PREMIUM_SOURCE,
        ToolRequest(
            TOOL_ID,
            {
                "source_id": PREMIUM_SOURCE.source_id,
                "source_version": PREMIUM_SOURCE.version,
                "query": "summary",
            },
        ),
    )


def test_installed_premium_wheel_denies_without_entitlement(tmp_path: Path) -> None:
    provider = InMemoryEntitlementProvider(lambda: NOW)
    environment = _environment(provider, tmp_path)
    try:
        premium = environment.registry.loaded_extensions[PLUGIN_ID]
        with pytest.raises(EntitlementDeniedError) as error:
            environment.dispatcher.dispatch(
                "tenant-a",
                _premium_call(),
                deadline_monotonic=10**10,
                maximum_results=1,
            )
        assert error.value.reason_code == EntitlementReason.NOT_FOUND.value
        assert premium.run_calls == 0
        assert provider.audit_events[-1].decision.reason is EntitlementReason.NOT_FOUND
    finally:
        environment.close()


def test_missing_entitlement_provider_fails_closed(tmp_path: Path) -> None:
    environment = _environment(None, tmp_path)
    try:
        premium = environment.registry.loaded_extensions[PLUGIN_ID]
        with pytest.raises(EntitlementDeniedError) as error:
            environment.dispatcher.dispatch(
                "tenant-a",
                _premium_call(),
                deadline_monotonic=10**10,
                maximum_results=1,
            )
        assert error.value.reason_code == EntitlementReason.PROVIDER_UNAVAILABLE.value
        assert premium.run_calls == 0
    finally:
        environment.close()


def test_grant_revoke_and_free_tool_continuity(tmp_path: Path) -> None:
    provider = InMemoryEntitlementProvider(lambda: NOW)
    environment = _environment(provider, tmp_path)
    try:
        provider.grant("tenant-a", ENTITLEMENT_ID, DEPLOYMENT)
        evidence = environment.dispatcher.dispatch(
            "tenant-a",
            _premium_call(),
            deadline_monotonic=10**10,
            maximum_results=1,
        )
        premium = environment.registry.loaded_extensions[PLUGIN_ID]
        assert evidence.source_id == PREMIUM_SOURCE.source_id
        assert evidence.operation == TOOL_ID
        assert evidence.value == {"result": "Synthetic premium demo result."}
        assert premium.run_calls == 1

        provider.revoke("tenant-a", ENTITLEMENT_ID, DEPLOYMENT)
        with pytest.raises(EntitlementDeniedError) as error:
            environment.dispatcher.dispatch(
                "tenant-a",
                _premium_call(),
                deadline_monotonic=10**10,
                maximum_results=1,
            )
        assert error.value.reason_code == EntitlementReason.REVOKED.value
        assert premium.run_calls == 1

        document_call = PlannedToolCall(
            "documents",
            DOCUMENT_SOURCE,
            ToolRequest(
                "documents.search",
                {
                    "source_id": DOCUMENT_SOURCE.source_id,
                    "source_version": DOCUMENT_SOURCE.version,
                    "query": "payment",
                },
            ),
        )
        free_evidence = environment.dispatcher.dispatch(
            "tenant-a",
            document_call,
            deadline_monotonic=10**10,
            maximum_results=1,
        )
        assert free_evidence.operation == "documents.search"
        assert "30 days" in free_evidence.value

        database_call = PlannedToolCall(
            "databases",
            DATABASE_SOURCE,
            ToolRequest(
                "databases.query",
                {
                    "source_id": DATABASE_SOURCE.source_id,
                    "source_version": DATABASE_SOURCE.version,
                    "query_id": "facts.list",
                    "filters": {},
                },
            ),
        )
        sql_evidence = environment.dispatcher.dispatch(
            "tenant-a",
            database_call,
            deadline_monotonic=10**10,
            maximum_results=1,
        )
        assert sql_evidence.operation == "databases.query"
        assert isinstance(sql_evidence.value, dict)
        assert sql_evidence.value["rows"] == [
            {"facts__id": 1, "facts__label": "Synthetic SQL fact"}
        ]
        assert len(provider.audit_events) == 2
        assert [event.decision.reason for event in provider.audit_events] == [
            EntitlementReason.GRANTED,
            EntitlementReason.REVOKED,
        ]
    finally:
        environment.close()


def test_expiry_and_cross_tenant_source_denial(tmp_path: Path) -> None:
    provider = InMemoryEntitlementProvider(lambda: NOW)
    provider.grant(
        "tenant-a",
        ENTITLEMENT_ID,
        DEPLOYMENT,
        expires_at=NOW - timedelta(seconds=1),
    )
    environment = _environment(provider, tmp_path)
    try:
        with pytest.raises(EntitlementDeniedError) as error:
            environment.dispatcher.dispatch(
                "tenant-a",
                _premium_call(),
                deadline_monotonic=10**10,
                maximum_results=1,
            )
        assert error.value.reason_code == EntitlementReason.EXPIRED.value

        checks_before = len(provider.audit_events)
        with pytest.raises(SourceAuthorizationError):
            environment.dispatcher.dispatch(
                "tenant-b",
                _premium_call(),
                deadline_monotonic=10**10,
                maximum_results=1,
            )
        assert len(provider.audit_events) == checks_before
        assert provider.check(
            "tenant-b", ENTITLEMENT_ID, DEPLOYMENT
        ).reason is EntitlementReason.TENANT_MISMATCH
    finally:
        environment.close()


def test_wrong_deployment_is_a_distinct_audited_denial() -> None:
    provider = InMemoryEntitlementProvider(lambda: NOW)
    provider.grant("tenant-a", ENTITLEMENT_ID, "other-host")

    decision = provider.check("tenant-a", ENTITLEMENT_ID, DEPLOYMENT)

    assert decision == EntitlementDecision(
        False, EntitlementReason.DEPLOYMENT_MISMATCH
    )
    assert provider.audit_events[-1].deployment == DEPLOYMENT
