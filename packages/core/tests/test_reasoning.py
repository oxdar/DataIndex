from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from dataclasses import asdict
from typing import Sequence

import pytest

from dataindex.catalog import SQLiteCatalog
from dataindex.entitlements import EntitlementDecision, EntitlementReason
from dataindex.exceptions import (
    EntitlementDeniedError,
    InvalidCitationError,
    InvalidToolEvidenceError,
    InvalidToolRequestError,
    ReasoningBudgetExceededError,
    SourceAuthorizationError,
    ToolNotAvailableError,
)
from dataindex.models import (
    EvidenceKind,
    EvidenceRecord,
    ExtensionManifest,
    HealthResult,
    HealthStatus,
    IndexContext,
    NativeLocator,
    Node,
    ProbeResult,
    RetrievalContext,
    RetrievalToolSpec,
    SourceRef,
    ToolRequest,
)
from dataindex.reasoning import (
    GroundedClaim,
    PlannedToolCall,
    PlannerTurn,
    ReasoningBudget,
    ReasoningEngine,
    ToolDispatcher,
)
from dataindex.registry import ExtensionRegistry
from dataindex.sdk import DataIndexClient


SOURCE = SourceRef("invoices", "text/markdown", "rev-1")


class FakeExtension:
    def __init__(self) -> None:
        self._commercial = False
        self._initialize_manifest()
        self.run_calls = 0
        self.bad_evidence = False
        self.bad_locator = False

    def as_commercial(self) -> None:
        self._commercial = True
        self._initialize_manifest()

    def _initialize_manifest(self) -> None:
        self._manifest = ExtensionManifest(
            plugin_id="fixture",
            distribution="dataindex-ext-fixture",
            plugin_version="0.1.0",
            core_api_range=">=0.1,<0.2",
            edition="commercial" if self._commercial else "free",
            license_mode="entitlement" if self._commercial else "none",
            capabilities=("document.index", "document.retrieve"),
            source_types=("text/markdown",),
            publisher="test",
            capability_entitlements=(
                {
                    "document.index": "fixture.index",
                    "document.retrieve": "fixture.pro",
                }
                if self._commercial
                else {}
            ),
        )

    def manifest(self) -> ExtensionManifest:
        return self._manifest

    def probe(self, source: SourceRef) -> ProbeResult:
        return ProbeResult(source.source_type == "text/markdown")

    def iter_index_records(
        self, source: SourceRef, ctx: IndexContext
    ) -> Iterator[Node]:
        yield Node(
            node_id="invoices",
            source_id=source.source_id,
            source_version=source.version,
            kind="document",
            title="Invoices",
            locator=NativeLocator("fixture", 1, {"section": "root"}),
        )

    def tools(self) -> Sequence[RetrievalToolSpec]:
        return (
            RetrievalToolSpec(
                tool_id="documents.search",
                input_schema={
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string", "minLength": 1},
                        "source_version": {"type": "string", "minLength": 1},
                        "query": {"type": "string", "minLength": 1},
                    },
                    "required": ["source_id", "source_version", "query"],
                    "additionalProperties": False,
                },
                capability="document.retrieve",
                maximum_results=1,
            ),
        )

    def run_tool(
        self, request: ToolRequest, ctx: RetrievalContext
    ) -> EvidenceRecord:
        self.run_calls += 1
        return EvidenceRecord(
            source_id="different" if self.bad_evidence else SOURCE.source_id,
            source_version=SOURCE.version,
            node_id="payment-terms" if self.bad_locator else None,
            locator=NativeLocator(
                "fixture-result", 1, {"query": request.parameters["query"]}
            ),
            operation=request.tool_id,
            value="Invoice payment terms are net 30 days.",
            evidence_kind=EvidenceKind.OBSERVED,
            execution_id=str(uuid.uuid4()),
        )

    def health(self) -> HealthResult:
        return HealthResult(HealthStatus.HEALTHY)

    def close(self) -> None:
        return None


class FakeDistribution:
    name = "dataindex-ext-fixture"
    version = "0.1.0"

    def __init__(self, manifest: ExtensionManifest) -> None:
        self.name = manifest.distribution
        self.version = manifest.plugin_version
        self.files = {
            "dataindex_ext_fixture/dataindex-extension.json": json.dumps(
                asdict(manifest)
            )
        }

    def read_text(self, path: str) -> str | None:
        return self.files.get(path)


class FakeEntryPoint:
    name = "fixture"
    value = "dataindex_ext_fixture.plugin:load_plugin"

    def __init__(self, extension: FakeExtension) -> None:
        self.extension = extension
        self.dist = FakeDistribution(extension.manifest())

    def load(self):
        return lambda: self.extension


class Allowlist:
    def __init__(
        self,
        *,
        tenant_id: str = "tenant-a",
        allowed_capabilities: set[str] | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.allowed_capabilities = (
            {"source.access", "document.index", "document.retrieve"}
            if allowed_capabilities is None
            else allowed_capabilities
        )

    def is_authorized(
        self, tenant_id: str, source: SourceRef, capability: str
    ) -> bool:
        return (
            tenant_id == self.tenant_id
            and source.source_id == SOURCE.source_id
            and capability in self.allowed_capabilities
        )


class NoEntitlements:
    def check(
        self, tenant_id: str, capability: str, deployment: str
    ) -> EntitlementDecision:
        return EntitlementDecision(False, EntitlementReason.NOT_FOUND)


class TwoTurnPlanner:
    def __init__(self, *, request: ToolRequest | None = None) -> None:
        self.request = request or ToolRequest(
            "documents.search",
            {
                "source_id": SOURCE.source_id,
                "source_version": SOURCE.version,
                "query": "payment terms",
            },
        )
        self.calls = 0

    def plan(
        self,
        *,
        question: str,
        tools: tuple,
        evidence: tuple[EvidenceRecord, ...],
        depth: int,
        remaining_tool_calls: int,
        remaining_tokens: int,
        deadline_monotonic: float,
    ) -> PlannerTurn:
        self.calls += 1
        if self.calls == 1:
            return PlannerTurn(
                tool_calls=(PlannedToolCall("fixture", SOURCE, self.request),),
                tokens_used=2,
            )
        return PlannerTurn(
            claims=(
                GroundedClaim(
                    "The invoices are due in 30 days.",
                    (evidence[0].execution_id,),
                ),
            ),
            tokens_used=2,
        )


def build_engine(
    *,
    extension: FakeExtension | None = None,
    tenant_id: str = "tenant-a",
    allowed_capabilities: set[str] | None = None,
) -> tuple[
    ReasoningEngine, SQLiteCatalog, FakeExtension, SourceRef, ToolDispatcher
]:
    extension = extension or FakeExtension()
    registry = ExtensionRegistry(
        trusted_distributions={("dataindex-ext-fixture", "0.1.0")},
        enabled_plugin_ids={"fixture"},
    )
    registry.discover((FakeEntryPoint(extension),))
    registry.load_enabled()
    catalog = SQLiteCatalog()
    catalog.create_source(SOURCE, tenant_id=tenant_id)
    catalog.create_node(
        Node(
            node_id="invoices",
            source_id=SOURCE.source_id,
            source_version=SOURCE.version,
            kind="document",
            title="Invoices",
            locator=NativeLocator("fixture", 1, {"section": "root"}),
        )
    )
    catalog.create_node(
        Node(
            node_id="payment-terms",
            source_id=SOURCE.source_id,
            source_version=SOURCE.version,
            kind="section",
            title="Payment terms",
            parent_id="invoices",
            locator=NativeLocator("fixture", 1, {"section": "payment-terms"}),
        )
    )
    dispatcher = ToolDispatcher(
        registry,
        catalog,
        Allowlist(tenant_id=tenant_id, allowed_capabilities=allowed_capabilities),
        NoEntitlements(),
        deployment="test",
    )
    return ReasoningEngine(dispatcher, catalog), catalog, extension, SOURCE, dispatcher


def test_planner_runs_only_source_scoped_calls_and_requires_citations() -> None:
    engine, catalog, extension, source, _ = build_engine()
    planner = TwoTurnPlanner()

    result = engine.run_with_planner(
        tenant_id="tenant-a",
        question="When are invoices due under the payment terms?",
        sources=(source,),
        planner=planner,
    )

    assert len(result.tool_calls) == 1
    assert result.depth == 1
    assert result.tokens_used == 4
    assert result.answer.claims[0].text == "The invoices are due in 30 days."
    assert result.answer.claims[0].evidence_ids == (
        result.answer.evidence[0].execution_id,
    )
    assert extension.run_calls == 1
    assert catalog.get_source_tenant(source.source_id) == "tenant-a"
    catalog.close()


def test_sdk_registers_indexes_searches_retrieves_and_answers(monkeypatch, tmp_path) -> None:
    extension = FakeExtension()
    entry_point = FakeEntryPoint(extension)
    monkeypatch.setattr(
        "dataindex.registry.entry_points", lambda *, group: (entry_point,)
    )
    client = DataIndexClient.open(
        tmp_path / "sdk.sqlite",
        tenant_id="tenant-a",
        source_authorizer=Allowlist(),
        trusted_distributions=(("dataindex-ext-fixture", "0.1.0"),),
        enabled_plugin_ids=("fixture",),
        entitlement_provider=NoEntitlements(),
        deployment="sdk-test",
    )
    client.register_source(SOURCE)
    assert client.list_extensions()[0].trusted
    assert client.list_sources() == (SOURCE,)
    indexed_nodes = client.index(SOURCE, "fixture", maximum_records=1)
    assert indexed_nodes[0].title == "Invoices"
    assert client.index(SOURCE, "fixture", maximum_records=1) == indexed_nodes
    assert client.search(SOURCE, "Invoices")[0].node_id == "invoices"

    request = ToolRequest(
        "documents.search",
        {
            "source_id": SOURCE.source_id,
            "source_version": SOURCE.version,
            "query": "payment terms",
        },
    )
    evidence = client.retrieve(PlannedToolCall("fixture", SOURCE, request))
    assert evidence.source_id == SOURCE.source_id
    result = client.ask(
        "When are invoices due under the payment terms?",
        (SOURCE,),
        TwoTurnPlanner(request=request),
    )
    assert result.answer.claims[0].text == "The invoices are due in 30 days."
    client.close()


def test_sdk_requires_index_capability_authorization(monkeypatch, tmp_path) -> None:
    extension = FakeExtension()
    entry_point = FakeEntryPoint(extension)
    monkeypatch.setattr(
        "dataindex.registry.entry_points", lambda *, group: (entry_point,)
    )
    client = DataIndexClient.open(
        tmp_path / "sdk-index-policy.sqlite",
        tenant_id="tenant-a",
        source_authorizer=Allowlist(allowed_capabilities={"source.access"}),
        trusted_distributions=(("dataindex-ext-fixture", "0.1.0"),),
        enabled_plugin_ids=("fixture",),
        deployment="sdk-test",
    )
    client.register_source(SOURCE)
    with pytest.raises(SourceAuthorizationError):
        client.index(SOURCE, "fixture")
    assert client.search(SOURCE, "Invoices") == ()
    client.close()


def test_sdk_requires_source_access_before_registration() -> None:
    client = DataIndexClient.open(
        tenant_id="tenant-a",
        source_authorizer=Allowlist(allowed_capabilities=set()),
        deployment="sdk-test",
    )
    with pytest.raises(SourceAuthorizationError):
        client.register_source(SOURCE)
    assert client.list_sources() == ()
    client.close()


def test_sdk_rejects_source_not_owned_by_client_tenant() -> None:
    engine, catalog, _, source, _ = build_engine()
    client = DataIndexClient.from_components(
        catalog,
        ExtensionRegistry(),
        tenant_id="tenant-b",
        source_authorizer=Allowlist(tenant_id="tenant-b"),
        entitlement_provider=None,
        deployment="sdk-test",
    )
    assert client.get_source(source.source_id) is None
    assert client.list_sources() == ()
    with pytest.raises(SourceAuthorizationError):
        client.search(source, "Invoices")
    client.close()


def test_cross_tenant_source_is_not_disclosed_to_planner() -> None:
    engine, catalog, _, source, _ = build_engine()
    planner = TwoTurnPlanner()

    result = engine.run_with_planner(
        tenant_id="tenant-b",
        question="When are invoices due under the payment terms?",
        sources=(source,),
        planner=planner,
    )

    assert result.answer.abstained
    assert result.selected_sources == ()
    assert planner.calls == 0
    catalog.close()


def test_no_source_abstains_without_calling_the_planner() -> None:
    engine, catalog, _, _, _ = build_engine()
    planner = TwoTurnPlanner()

    result = engine.run_with_planner(
        tenant_id="tenant-a",
        question="What are the invoice payment terms?",
        sources=(),
        planner=planner,
    )

    assert result.answer.abstained
    assert result.answer.reason == "No authorized source matched the question."
    assert planner.calls == 0
    catalog.close()


def test_tool_capability_authorization_is_separate_from_source_access() -> None:
    engine, catalog, _, source, _ = build_engine(
        allowed_capabilities={"source.access"}
    )
    planner = TwoTurnPlanner()

    result = engine.run_with_planner(
        tenant_id="tenant-a",
        question="When are invoices due under the payment terms?",
        sources=(source,),
        planner=planner,
    )

    assert result.answer.abstained
    assert planner.calls == 0
    catalog.close()


def test_unhealthy_plugin_is_not_advertised_and_is_reported(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _, catalog, extension, source, dispatcher = build_engine()
    monkeypatch.setattr(
        extension,
        "health",
        lambda: HealthResult(HealthStatus.DEGRADED, "test degradation"),
    )

    assert dispatcher.available_tools("tenant-a", (source,)) == ()
    assert "Skipping plugin fixture during tool discovery" in caplog.text
    catalog.close()


def test_unsupported_extension_schema_is_rejected_during_discovery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class InvalidSchemaExtension(FakeExtension):
        def tools(self) -> Sequence[RetrievalToolSpec]:
            return (
                RetrievalToolSpec(
                    tool_id="documents.search",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "optional": {
                                "type": "string",
                                "pattern": ".*",
                            }
                        },
                        "additionalProperties": False,
                    },
                    capability="document.retrieve",
                    maximum_results=1,
                ),
            )

    extension = InvalidSchemaExtension()
    engine, catalog, _, source, dispatcher = build_engine(extension=extension)
    planner = TwoTurnPlanner()

    assert dispatcher.available_tools("tenant-a", (source,)) == ()
    result = engine.run_with_planner(
        tenant_id="tenant-a",
        question="When are invoices due under the payment terms?",
        sources=(source,),
        planner=planner,
    )

    assert result.answer.abstained
    assert planner.calls == 0
    assert "unsupported tool schema keywords" in caplog.text
    catalog.close()


def test_additional_tool_properties_are_rejected_before_execution() -> None:
    engine, catalog, extension, source, _ = build_engine()
    planner = TwoTurnPlanner(
        request=ToolRequest(
            "documents.search",
            {
                "source_id": source.source_id,
                "source_version": source.version,
                "query": "payment terms",
                "sql": "DROP TABLE invoices",
            },
        )
    )

    with pytest.raises(InvalidToolRequestError, match="not an allowed field"):
        engine.run_with_planner(
            tenant_id="tenant-a",
            question="When are invoices due under the payment terms?",
            sources=(source,),
            planner=planner,
        )

    assert extension.run_calls == 0
    catalog.close()


def test_tool_call_budget_is_checked_before_any_call_in_a_turn() -> None:
    engine, catalog, extension, source, _ = build_engine()

    class TwoCallsPlanner:
        def plan(
            self,
            *,
            question: str,
            tools: tuple,
            evidence: tuple[EvidenceRecord, ...],
            depth: int,
            remaining_tool_calls: int,
            remaining_tokens: int,
            deadline_monotonic: float,
        ) -> PlannerTurn:
            request = ToolRequest(
                "documents.search",
                {
                    "source_id": source.source_id,
                    "source_version": source.version,
                    "query": "payment terms",
                },
            )
            call = PlannedToolCall("fixture", source, request)
            return PlannerTurn(tool_calls=(call, call), tokens_used=1)

    with pytest.raises(ReasoningBudgetExceededError, match="tool-call count"):
        engine.run_with_planner(
            tenant_id="tenant-a",
            question="When are invoices due under the payment terms?",
            sources=(source,),
            planner=TwoCallsPlanner(),
            budget=ReasoningBudget(max_tool_calls=1),
        )
    assert extension.run_calls == 0
    catalog.close()


def test_token_budget_is_enforced_before_tool_execution() -> None:
    engine, catalog, extension, source, _ = build_engine()
    planner = TwoTurnPlanner()

    with pytest.raises(ReasoningBudgetExceededError, match="token budget"):
        engine.run_with_planner(
            tenant_id="tenant-a",
            question="When are invoices due under the payment terms?",
            sources=(source,),
            planner=planner,
            budget=ReasoningBudget(max_tokens=1),
        )
    assert extension.run_calls == 0
    catalog.close()


def test_depth_budget_is_enforced_before_tool_execution() -> None:
    engine, catalog, extension, source, _ = build_engine()
    with pytest.raises(ReasoningBudgetExceededError, match="depth"):
        engine.run_with_planner(
            tenant_id="tenant-a",
            question="When are invoices due under the payment terms?",
            sources=(source,),
            planner=TwoTurnPlanner(),
            budget=ReasoningBudget(max_depth=0),
        )
    assert extension.run_calls == 0
    catalog.close()


def test_late_planner_result_is_rejected() -> None:
    engine, catalog, _, source, _ = build_engine()

    class SlowPlanner:
        def plan(
            self,
            *,
            question: str,
            tools: tuple,
            evidence: tuple[EvidenceRecord, ...],
            depth: int,
            remaining_tool_calls: int,
            remaining_tokens: int,
            deadline_monotonic: float,
        ) -> PlannerTurn:
            time.sleep(0.02)
            return PlannerTurn(tokens_used=1)

    with pytest.raises(ReasoningBudgetExceededError, match="deadline"):
        engine.run_with_planner(
            tenant_id="tenant-a",
            question="When are invoices due under the payment terms?",
            sources=(source,),
            planner=SlowPlanner(),
            budget=ReasoningBudget(timeout_seconds=0.005),
        )
    catalog.close()


def test_missing_entitlement_denies_commercial_tool_execution() -> None:
    extension = FakeExtension()
    extension.as_commercial()
    _, catalog, _, source, dispatcher = build_engine(extension=extension)
    call = PlannedToolCall(
        "fixture",
        source,
        ToolRequest(
            "documents.search",
            {
                "source_id": source.source_id,
                "source_version": source.version,
                "query": "payment terms",
            },
        ),
    )

    with pytest.raises(EntitlementDeniedError) as error:
        dispatcher.dispatch(
            "tenant-a",
            call,
            deadline_monotonic=10**10,
            maximum_results=1,
        )

    assert extension.run_calls == 0
    assert error.value.reason_code == EntitlementReason.NOT_FOUND.value
    catalog.close()


def test_untrusted_or_missing_plugin_tool_is_denied() -> None:
    engine, catalog, _, source, _ = build_engine()
    call = PlannedToolCall(
        "untrusted",
        source,
        ToolRequest(
            "documents.search",
            {
                "source_id": source.source_id,
                "source_version": source.version,
                "query": "payment terms",
            },
        ),
    )
    with pytest.raises(ToolNotAvailableError):
        engine.run_deterministic(
            tenant_id="tenant-a",
            question="payment terms",
            sources=(source,),
            tool_calls=(call,),
        )
    catalog.close()


def test_invalid_tool_provenance_is_rejected() -> None:
    extension = FakeExtension()
    extension.bad_evidence = True
    engine, catalog, _, source, _ = build_engine(extension=extension)
    planner = TwoTurnPlanner()

    with pytest.raises(InvalidToolEvidenceError, match="evidence source"):
        engine.run_with_planner(
            tenant_id="tenant-a",
            question="When are invoices due under the payment terms?",
            sources=(source,),
            planner=planner,
        )
    catalog.close()


def test_hallucinated_catalog_locator_is_rejected() -> None:
    extension = FakeExtension()
    extension.bad_locator = True
    engine, catalog, _, source, _ = build_engine(extension=extension)

    with pytest.raises(InvalidToolEvidenceError, match="locator"):
        engine.run_with_planner(
            tenant_id="tenant-a",
            question="When are invoices due under the payment terms?",
            sources=(source,),
            planner=TwoTurnPlanner(),
        )
    catalog.close()


def test_claim_cannot_cite_missing_evidence() -> None:
    engine, catalog, _, source, _ = build_engine()

    class UnsupportedClaimPlanner:
        def plan(
            self,
            *,
            question: str,
            tools: tuple,
            evidence: tuple[EvidenceRecord, ...],
            depth: int,
            remaining_tool_calls: int,
            remaining_tokens: int,
            deadline_monotonic: float,
        ) -> PlannerTurn:
            return PlannerTurn(
                claims=(GroundedClaim("Unsupported.", ("missing-evidence",)),),
                tokens_used=1,
            )

    with pytest.raises(InvalidCitationError):
        engine.run_with_planner(
            tenant_id="tenant-a",
            question="When are invoices due under the payment terms?",
            sources=(source,),
            planner=UnsupportedClaimPlanner(),
        )
    catalog.close()


def test_direct_dispatch_rechecks_source_authorization() -> None:
    _, catalog, _, source, dispatcher = build_engine(
        allowed_capabilities={"document.retrieve"}
    )
    call = PlannedToolCall(
        "fixture",
        source,
        ToolRequest(
            "documents.search",
            {
                "source_id": source.source_id,
                "source_version": source.version,
                "query": "payment terms",
            },
        ),
    )
    with pytest.raises(SourceAuthorizationError):
        dispatcher.dispatch(
            "tenant-a",
            call,
            deadline_monotonic=10**10,
            maximum_results=1,
        )
    catalog.close()
