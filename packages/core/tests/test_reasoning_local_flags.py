from __future__ import annotations

import json
import logging
import math
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass

import pytest

from dataindex.catalog import SQLiteCatalog
from dataindex.exceptions import (
    InvalidToolEvidenceError,
    InvalidToolRequestError,
    ReasoningBudgetExceededError,
    SourceAuthorizationError,
    StaleSourceVersionError,
    ToolEvidenceTooLargeError,
    ToolExecutionError,
    ToolNotAvailableError,
)
from dataindex.models import (
    EvidenceKind,
    EvidenceRecord,
    ExtensionManifest,
    HealthResult,
    HealthStatus,
    IndexContext,
    JsonValue,
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
    HierarchicalSourcePlanner,
    PlannedToolCall,
    PlannerTurn,
    ReasoningBudget,
    ReasoningEngine,
    ReasoningResult,
    SourceCandidate,
    ToolCandidate,
    ToolDispatcher,
    validate_tool_parameters,
)
from dataindex.registry import ExtensionRegistry

TENANT = "tenant-a"
HANDBOOK = SourceRef("handbook", "text/markdown", "rev-7")
LEDGER = SourceRef("ledger", "text/markdown", "rev-2")
RESTRICTED = SourceRef("restricted", "text/markdown", "rev-1")
FOREIGN = SourceRef("foreign", "text/markdown", "rev-1")
QUESTION = "How long is the refund window?"
QUESTION_WITHOUT_CONTENT_WORDS = "What about that?"
SECRET = "sk-live-SECRET C:\\private\\payroll.db"
KIB = 1024

SEARCH_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "source_id": {"type": "string", "minLength": 1},
        "source_version": {"type": "string", "minLength": 1},
        "query": {"type": "string", "minLength": 1},
    },
    "required": ["source_id", "source_version", "query"],
    "additionalProperties": False,
}

Handler = Callable[[ToolRequest], EvidenceRecord]


def evidence_for(
    request: ToolRequest,
    value: JsonValue = "Refunds close after 30 days.",
    *,
    source_id: str | None = None,
    source_version: str | None = None,
    operation: str | None = None,
) -> EvidenceRecord:
    return EvidenceRecord(
        source_id=source_id or str(request.parameters["source_id"]),
        source_version=source_version or str(request.parameters["source_version"]),
        locator=NativeLocator(
            "fixture", 1, {"query": str(request.parameters["query"])}
        ),
        operation=operation or request.tool_id,
        value=value,
        evidence_kind=EvidenceKind.OBSERVED,
        execution_id=str(uuid.uuid4()),
    )


def raising(error: BaseException) -> Handler:
    def handler(request: ToolRequest) -> EvidenceRecord:
        raise error

    return handler


def by_query(**handlers: Handler) -> Handler:
    """Route requests by their query; unlisted queries return normal evidence."""

    def handler(request: ToolRequest) -> EvidenceRecord:
        return handlers.get(str(request.parameters["query"]), evidence_for)(request)

    return handler


class ScriptedPlugin:
    """Free plugin whose tool behavior and schema are replaced per test."""

    def __init__(self) -> None:
        self.handler: Handler = evidence_for
        self.schema: Mapping[str, JsonValue] = SEARCH_SCHEMA
        self.requests: list[ToolRequest] = []
        self.contexts: list[RetrievalContext] = []
        self._manifest = ExtensionManifest(
            plugin_id="docs",
            distribution="dataindex-ext-docs",
            plugin_version="0.1.0",
            core_api_range=">=0.1,<0.2",
            edition="free",
            license_mode="none",
            capabilities=("document.retrieve",),
            source_types=("text/markdown",),
            publisher="test",
        )

    def manifest(self) -> ExtensionManifest:
        return self._manifest

    def probe(self, source: SourceRef) -> ProbeResult:
        return ProbeResult(source.source_type == "text/markdown")

    def iter_index_records(
        self, source: SourceRef, ctx: IndexContext
    ) -> Iterator[Node]:
        return iter(())

    def tools(self) -> Sequence[RetrievalToolSpec]:
        return (
            RetrievalToolSpec(
                tool_id="documents.search",
                input_schema=self.schema,
                capability="document.retrieve",
                maximum_results=5,
            ),
        )

    def run_tool(self, request: ToolRequest, ctx: RetrievalContext) -> EvidenceRecord:
        self.requests.append(request)
        self.contexts.append(ctx)
        return self.handler(request)

    def health(self) -> HealthResult:
        return HealthResult(HealthStatus.HEALTHY)

    def close(self) -> None:
        return None


class FakeDistribution:
    def __init__(self, manifest: ExtensionManifest) -> None:
        self.name = manifest.distribution
        self.version = manifest.plugin_version
        self.files = {
            "dataindex_ext_docs/dataindex-extension.json": json.dumps(asdict(manifest))
        }

    def read_text(self, path: str) -> str | None:
        return self.files.get(path)


class FakeEntryPoint:
    name = "docs"
    value = "dataindex_ext_docs.plugin:load_plugin"

    def __init__(self, plugin: ScriptedPlugin) -> None:
        self.plugin = plugin
        self.dist = FakeDistribution(plugin.manifest())

    def load(self) -> Callable[[], ScriptedPlugin]:
        return lambda: self.plugin


class Allowlist:
    def __init__(self, tenant_id: str, source_ids: set[str]) -> None:
        self.tenant_id = tenant_id
        self.source_ids = source_ids

    def is_authorized(
        self, tenant_id: str, source: SourceRef, capability: str
    ) -> bool:
        return tenant_id == self.tenant_id and source.source_id in self.source_ids


@dataclass(frozen=True)
class PlanRequest:
    tools: tuple[ToolCandidate, ...]
    evidence: tuple[EvidenceRecord, ...]
    depth: int
    remaining_tool_calls: int
    remaining_tokens: int


Step = Callable[[tuple[EvidenceRecord, ...]], PlannerTurn]


class ScriptedPlanner:
    """Replays prepared turns; a callable step receives the evidence gathered so far."""

    def __init__(self, *steps: PlannerTurn | Step) -> None:
        self._steps = list(steps)
        self.requests: list[PlanRequest] = []

    def plan(
        self,
        *,
        question: str,
        tools: tuple[ToolCandidate, ...],
        evidence: tuple[EvidenceRecord, ...],
        depth: int,
        remaining_tool_calls: int,
        remaining_tokens: int,
        deadline_monotonic: float,
    ) -> PlannerTurn:
        self.requests.append(
            PlanRequest(tools, evidence, depth, remaining_tool_calls, remaining_tokens)
        )
        if not self._steps:
            raise AssertionError("the planner was consulted more often than scripted")
        step = self._steps.pop(0)
        return step(evidence) if callable(step) else step


class ReportingPlanner(ScriptedPlanner):
    def __init__(self, *steps: PlannerTurn | Step) -> None:
        super().__init__(*steps)
        self.failures: list[tuple[PlannedToolCall, str]] = []

    def report_tool_failure(self, call: PlannedToolCall, error_type: str) -> None:
        self.failures.append((call, error_type))


def search(query: str, *, source: SourceRef = HANDBOOK) -> PlannedToolCall:
    return PlannedToolCall(
        "docs",
        source,
        ToolRequest(
            "documents.search",
            {
                "source_id": source.source_id,
                "source_version": source.version,
                "query": query,
            },
        ),
    )


def call_turn(*queries: str) -> PlannerTurn:
    return PlannerTurn(
        tool_calls=tuple(search(query) for query in queries), tokens_used=1
    )


def answer_turn(text: str = "Refunds close after 30 days.") -> Step:
    return lambda evidence: PlannerTurn(
        claims=(GroundedClaim(text, (evidence[0].execution_id,)),), tokens_used=1
    )


ABSTAIN = PlannerTurn(tokens_used=1)


def add_source(
    catalog: SQLiteCatalog,
    source: SourceRef,
    *titles: str,
    tenant_id: str = TENANT,
) -> None:
    catalog.create_source(source, tenant_id=tenant_id)
    catalog.upsert_nodes(
        [node(source, f"{source.source_id}-{index}", title) for index, title in enumerate(titles)]
    )


def node(
    source: SourceRef, node_id: str, title: str, *, parent_id: str | None = None
) -> Node:
    return Node(
        node_id=node_id,
        source_id=source.source_id,
        source_version=source.version,
        kind="section",
        title=title,
        locator=NativeLocator("fixture", 1, {"id": node_id}),
        parent_id=parent_id,
    )


@dataclass
class World:
    catalog: SQLiteCatalog
    plugin: ScriptedPlugin
    authorizer: Allowlist
    dispatcher: ToolDispatcher
    engine: ReasoningEngine


@pytest.fixture
def catalog() -> Iterator[SQLiteCatalog]:
    store = SQLiteCatalog()
    yield store
    store.close()


@pytest.fixture
def world(catalog: SQLiteCatalog) -> World:
    plugin = ScriptedPlugin()
    registry = ExtensionRegistry(
        trusted_distributions={("dataindex-ext-docs", "0.1.0")},
        enabled_plugin_ids={"docs"},
    )
    registry.discover((FakeEntryPoint(plugin),))
    registry.load_enabled()
    add_source(catalog, HANDBOOK, "Refund policy", "Return window")
    add_source(catalog, LEDGER, "Quarterly totals")
    add_source(catalog, RESTRICTED, "Refund exceptions")
    add_source(catalog, FOREIGN, "Refund archive", tenant_id="tenant-b")
    authorizer = Allowlist(TENANT, {"handbook", "ledger"})
    dispatcher = ToolDispatcher(registry, catalog, authorizer, deployment="test")
    return World(
        catalog, plugin, authorizer, dispatcher, ReasoningEngine(dispatcher, catalog)
    )


def run(
    world: World,
    planner: ScriptedPlanner,
    *,
    tenant_id: str = TENANT,
    question: str = QUESTION,
    sources: Sequence[SourceRef] = (HANDBOOK,),
    budget: ReasoningBudget | None = None,
    require_source_match: bool = True,
    tolerate_tool_failures: bool = False,
) -> ReasoningResult:
    return world.engine.run_with_planner(
        tenant_id=tenant_id,
        question=question,
        sources=tuple(sources),
        planner=planner,
        budget=budget or ReasoningBudget(),
        require_source_match=require_source_match,
        tolerate_tool_failures=tolerate_tool_failures,
    )


def dispatch(
    world: World,
    call: PlannedToolCall,
    *,
    seconds: float = 30.0,
    maximum_results: int = 10,
) -> EvidenceRecord:
    return world.dispatcher.dispatch(
        TENANT,
        call,
        deadline_monotonic=time.monotonic() + seconds,
        maximum_results=maximum_results,
    )


def queries(requests: Sequence[ToolRequest]) -> list[str]:
    return [str(request.parameters["query"]) for request in requests]


# --- tolerate_tool_failures ------------------------------------------------------


def test_a_failing_tool_ends_the_run_by_default_without_leaking_its_message(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.plugin.handler = raising(RuntimeError(SECRET))
    planner = ReportingPlanner(call_turn("refunds"))

    with caplog.at_level(logging.WARNING):
        with pytest.raises(ToolExecutionError) as raised:
            run(world, planner)

    assert raised.value.error_type == "RuntimeError"
    assert "RuntimeError" in str(raised.value)
    assert SECRET not in str(raised.value)
    assert "RuntimeError" in caplog.text
    assert SECRET not in caplog.text
    assert planner.failures == []
    assert len(world.plugin.requests) == 1


def test_a_tolerated_failure_is_reported_and_the_run_continues(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.plugin.handler = by_query(broken=raising(RuntimeError(SECRET)))
    planner = ReportingPlanner(call_turn("broken"), call_turn("refunds"), answer_turn())

    with caplog.at_level(logging.WARNING):
        result = run(
            world,
            planner,
            budget=ReasoningBudget(max_tool_calls=5),
            tolerate_tool_failures=True,
        )

    assert planner.failures == [(search("broken"), "RuntimeError")]
    assert [item.remaining_tool_calls for item in planner.requests] == [5, 4, 3]
    assert [len(item.evidence) for item in planner.requests] == [0, 0, 1]
    assert queries(world.plugin.requests) == ["broken", "refunds"]
    assert [call.request.parameters["query"] for call in result.tool_calls] == [
        "broken",
        "refunds",
    ]
    assert result.depth == 2
    assert not result.answer.abstained
    assert result.answer.claims[0].evidence_ids == (
        result.answer.evidence[0].execution_id,
    )
    assert SECRET not in caplog.text


def test_the_other_calls_of_a_turn_still_run_after_a_tolerated_failure(
    world: World,
) -> None:
    world.plugin.handler = by_query(second=raising(ValueError(SECRET)))
    planner = ReportingPlanner(call_turn("first", "second", "third"), answer_turn())

    result = run(world, planner, tolerate_tool_failures=True)

    assert queries(world.plugin.requests) == ["first", "second", "third"]
    assert planner.failures == [(search("second"), "ValueError")]
    assert len(planner.requests[1].evidence) == 2
    assert len(result.tool_calls) == 3
    assert len(result.answer.evidence) == 2


def test_oversized_evidence_is_a_tolerated_failure_too(world: World) -> None:
    world.plugin.handler = by_query(
        huge=lambda request: evidence_for(request, "x" * (300 * KIB))
    )
    planner = ReportingPlanner(call_turn("huge"), call_turn("refunds"), answer_turn())

    result = run(world, planner, tolerate_tool_failures=True)

    assert planner.failures == [(search("huge"), "ToolEvidenceTooLargeError")]
    assert len(result.answer.evidence) == 1
    assert result.answer.evidence[0].value == "Refunds close after 30 days."


def test_oversized_evidence_ends_the_run_by_default(world: World) -> None:
    world.plugin.handler = by_query(
        huge=lambda request: evidence_for(request, "x" * (300 * KIB))
    )

    with pytest.raises(ToolEvidenceTooLargeError):
        run(world, ReportingPlanner(call_turn("huge")))


def test_a_planner_without_a_failure_hook_still_completes_a_tolerant_run(
    world: World,
) -> None:
    world.plugin.handler = by_query(broken=raising(RuntimeError(SECRET)))
    planner = ScriptedPlanner(call_turn("broken"), call_turn("refunds"), answer_turn())
    assert not hasattr(planner, "report_tool_failure")

    result = run(world, planner, tolerate_tool_failures=True)

    assert not result.answer.abstained
    assert queries(world.plugin.requests) == ["broken", "refunds"]


def test_failed_calls_still_use_up_the_tool_call_budget(world: World) -> None:
    world.plugin.handler = raising(RuntimeError(SECRET))
    planner = ReportingPlanner(call_turn("a"), call_turn("b"), call_turn("c"))

    with pytest.raises(ReasoningBudgetExceededError, match="tool-call count"):
        run(
            world,
            planner,
            budget=ReasoningBudget(max_tool_calls=2),
            tolerate_tool_failures=True,
        )

    assert [item.remaining_tool_calls for item in planner.requests] == [2, 1, 0]
    assert [call.request.parameters["query"] for call, _ in planner.failures] == [
        "a",
        "b",
    ]
    assert queries(world.plugin.requests) == ["a", "b"]


@pytest.mark.parametrize(
    "handler",
    [
        pytest.param(
            lambda request: evidence_for(request, source_id="ledger"),
            id="evidence-of-another-source",
        ),
        pytest.param(
            lambda request: evidence_for(request, source_version="rev-0"),
            id="evidence-of-another-revision",
        ),
        pytest.param(
            lambda request: evidence_for(request, operation="documents.other"),
            id="evidence-of-another-operation",
        ),
    ],
)
def test_forged_provenance_is_never_tolerated(
    world: World, handler: Handler
) -> None:
    world.plugin.handler = handler
    planner = ReportingPlanner(call_turn("refunds"), answer_turn())

    with pytest.raises(InvalidToolEvidenceError, match="does not match") as raised:
        run(world, planner, tolerate_tool_failures=True)

    assert type(raised.value) is InvalidToolEvidenceError
    assert planner.failures == []
    assert len(planner.requests) == 1


@pytest.mark.parametrize(
    "error",
    [
        SourceAuthorizationError("the plugin refused"),
        MemoryError(),
        RecursionError("too deep"),
    ],
    ids=lambda error: type(error).__name__,
)
def test_only_tool_failures_are_tolerated(world: World, error: BaseException) -> None:
    world.plugin.handler = raising(error)
    planner = ReportingPlanner(call_turn("refunds"), answer_turn())

    with pytest.raises(type(error)) as raised:
        run(world, planner, tolerate_tool_failures=True)

    assert raised.value is error
    assert planner.failures == []


def test_revoked_access_ends_a_tolerant_run_before_the_next_call(world: World) -> None:
    def revoke_access(request: ToolRequest) -> EvidenceRecord:
        world.authorizer.source_ids.discard("handbook")
        return evidence_for(request)

    world.plugin.handler = revoke_access
    planner = ReportingPlanner(call_turn("first", "second"))

    with pytest.raises(SourceAuthorizationError, match="not authorized"):
        run(world, planner, tolerate_tool_failures=True)

    assert queries(world.plugin.requests) == ["first"]
    assert planner.failures == []


def test_a_source_that_changes_during_a_tool_call_ends_a_tolerant_run(
    world: World,
) -> None:
    def move_source(request: ToolRequest) -> EvidenceRecord:
        world.catalog.update_source(SourceRef("handbook", "text/markdown", "rev-8"))
        return evidence_for(request)

    world.plugin.handler = move_source
    planner = ReportingPlanner(call_turn("refunds"), answer_turn())

    with pytest.raises(StaleSourceVersionError):
        run(world, planner, tolerate_tool_failures=True)

    assert planner.failures == []


def test_a_slow_tool_ends_a_tolerant_run_with_a_deadline_error(world: World) -> None:
    def slow(request: ToolRequest) -> EvidenceRecord:
        time.sleep(0.2)
        return evidence_for(request)

    world.plugin.handler = slow
    planner = ReportingPlanner(call_turn("refunds"), answer_turn())

    with pytest.raises(ReasoningBudgetExceededError, match="deadline"):
        run(
            world,
            planner,
            budget=ReasoningBudget(timeout_seconds=0.05),
            tolerate_tool_failures=True,
        )

    assert planner.failures == []


# --- dispatcher: plugin exceptions and evidence size --------------------------------


class PluginBoom(Exception):
    pass


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError(SECRET),
        ValueError(SECRET),
        KeyError(SECRET),
        OSError(SECRET),
        TimeoutError(SECRET),
        ZeroDivisionError(SECRET),
        PluginBoom(SECRET),
    ],
    ids=lambda error: type(error).__name__,
)
def test_plugin_exceptions_are_wrapped_without_their_message(
    world: World, error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    world.plugin.handler = raising(error)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(ToolExecutionError) as raised:
            dispatch(world, search("refunds"))

    assert raised.value.error_type == type(error).__name__
    assert type(error).__name__ in str(raised.value)
    assert "documents.search" in str(raised.value)
    assert SECRET not in str(raised.value)
    assert SECRET not in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        SourceAuthorizationError("the plugin refused"),
        InvalidToolRequestError("the plugin disliked the request"),
        MemoryError(),
        RecursionError("too deep"),
    ],
    ids=lambda error: type(error).__name__,
)
def test_framework_and_resource_errors_pass_through_the_dispatcher_unwrapped(
    world: World, error: BaseException
) -> None:
    world.plugin.handler = raising(error)

    with pytest.raises(type(error)) as raised:
        dispatch(world, search("refunds"))

    assert raised.value is error


def test_evidence_within_the_size_limit_is_returned(world: World) -> None:
    world.plugin.handler = lambda request: evidence_for(request, "x" * (200 * KIB))

    record = dispatch(world, search("refunds"))

    assert len(record.value) == 200 * KIB


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("x" * (300 * KIB), id="300KiB-text"),
        pytest.param("x" * (256 * KIB), id="text-as-large-as-the-limit"),
        pytest.param(["row-0123456789"] * 20_000, id="large-json-list"),
    ],
)
def test_evidence_over_the_size_limit_is_rejected(
    world: World, value: JsonValue
) -> None:
    world.plugin.handler = lambda request: evidence_for(request, value)

    with pytest.raises(ToolEvidenceTooLargeError, match="262144"):
        dispatch(world, search("refunds"))

    assert len(world.plugin.requests) == 1


def test_the_plugin_context_carries_the_smaller_of_the_budget_and_tool_limits(
    world: World,
) -> None:
    dispatch(world, search("a"), maximum_results=100)
    dispatch(world, search("b"), maximum_results=2)

    assert [item.maximum_results for item in world.plugin.contexts] == [5, 2]


@pytest.mark.parametrize("maximum_results", [0, -1, True, 2.5, "5", None])
def test_a_non_positive_result_limit_is_rejected_before_the_plugin_runs(
    world: World, maximum_results: object
) -> None:
    with pytest.raises(ValueError, match="maximum_results"):
        dispatch(world, search("refunds"), maximum_results=maximum_results)  # type: ignore[arg-type]

    assert world.plugin.requests == []


def test_an_expired_deadline_never_reaches_the_plugin(world: World) -> None:
    with pytest.raises(ReasoningBudgetExceededError, match="deadline"):
        dispatch(world, search("refunds"), seconds=-1)

    assert world.plugin.requests == []


# --- require_source_match ---------------------------------------------------------


def test_with_source_matching_only_relevant_sources_reach_the_planner(
    world: World,
) -> None:
    planner = ScriptedPlanner(ABSTAIN)

    result = run(world, planner, sources=(HANDBOOK, LEDGER))

    (request,) = planner.requests
    assert {item.source for item in request.tools} == {HANDBOOK}
    assert result.selected_sources == (HANDBOOK,)


def test_without_source_matching_every_authorized_source_reaches_the_planner(
    world: World,
) -> None:
    planner = ScriptedPlanner(ABSTAIN)

    result = run(
        world, planner, sources=(LEDGER, HANDBOOK), require_source_match=False
    )

    (request,) = planner.requests
    hints = {
        item.source.source_id: (item.source_score, item.matched_titles)
        for item in request.tools
    }
    assert hints == {
        "handbook": (6, ("Refund policy", "Return window")),
        "ledger": (0, ()),
    }
    assert result.selected_sources == (HANDBOOK, LEDGER)
    assert result.answer.abstained


def test_a_question_without_content_words_abstains_unless_matching_is_disabled(
    world: World,
) -> None:
    planner = ScriptedPlanner(ABSTAIN)

    strict = run(world, planner, question=QUESTION_WITHOUT_CONTENT_WORDS)

    assert strict.answer.abstained
    assert strict.answer.reason == "No authorized source matched the question."
    assert (strict.selected_sources, strict.tool_calls, strict.tokens_used) == ((), (), 0)
    assert planner.requests == []

    relaxed = run(
        world,
        planner,
        question=QUESTION_WITHOUT_CONTENT_WORDS,
        require_source_match=False,
    )

    (request,) = planner.requests
    assert [(item.source, item.source_score, item.matched_titles) for item in request.tools] == [
        (HANDBOOK, 0, ())
    ]
    assert relaxed.selected_sources == (HANDBOOK,)
    assert relaxed.answer.reason == "No source evidence supports an answer."


def test_disabling_source_matching_never_widens_access(world: World) -> None:
    unregistered = SourceRef("unregistered", "text/markdown", "rev-1")
    planner = ScriptedPlanner(ABSTAIN)

    run(
        world,
        planner,
        sources=(HANDBOOK, RESTRICTED, FOREIGN, unregistered),
        require_source_match=False,
    )

    (request,) = planner.requests
    assert {item.source.source_id for item in request.tools} == {"handbook"}


def test_without_any_accessible_source_the_planner_is_never_consulted(
    world: World,
) -> None:
    planner = ScriptedPlanner(ABSTAIN)

    result = run(
        world, planner, sources=(RESTRICTED, FOREIGN), require_source_match=False
    )

    assert planner.requests == []
    assert result.answer.abstained
    assert result.answer.reason == "No authorized source matched the question."


def test_a_relaxed_run_can_still_retrieve_and_cite(world: World) -> None:
    planner = ScriptedPlanner(call_turn("refunds"), answer_turn())

    result = run(
        world,
        planner,
        question=QUESTION_WITHOUT_CONTENT_WORDS,
        require_source_match=False,
    )

    assert not result.answer.abstained
    assert queries(world.plugin.requests) == ["refunds"]


def test_the_source_planner_keeps_unmatched_sources_only_when_asked(
    world: World,
) -> None:
    planner = HierarchicalSourcePlanner(world.catalog)
    matched = SourceCandidate(HANDBOOK, 6, ("Refund policy", "Return window"))

    assert planner.select(QUESTION, (HANDBOOK, LEDGER)) == (matched,)
    assert planner.select(QUESTION, (LEDGER, HANDBOOK), require_match=False) == (
        matched,
        SourceCandidate(LEDGER, 0, ()),
    )
    assert planner.select(QUESTION_WITHOUT_CONTENT_WORDS, (HANDBOOK,)) == ()
    assert planner.select(
        QUESTION_WITHOUT_CONTENT_WORDS, (LEDGER, HANDBOOK), require_match=False
    ) == (SourceCandidate(HANDBOOK, 0, ()), SourceCandidate(LEDGER, 0, ()))


@pytest.mark.parametrize("limit", [0, 65, -1, True, 2.5, "8", None])
def test_the_source_planner_limit_must_be_between_one_and_sixty_four(
    world: World, limit: object
) -> None:
    planner = HierarchicalSourcePlanner(world.catalog)

    with pytest.raises(ValueError, match="limit"):
        planner.select(QUESTION, (HANDBOOK,), limit=limit)  # type: ignore[arg-type]


# --- engine limits ----------------------------------------------------------------


@pytest.mark.parametrize("question", ["", "   \n", "x" * 2_049, None, 7])
def test_questions_outside_one_to_2048_characters_are_rejected_before_any_work(
    world: World, question: object
) -> None:
    planner = ScriptedPlanner(ABSTAIN)

    with pytest.raises(ValueError, match="question"):
        run(world, planner, question=question)  # type: ignore[arg-type]

    assert planner.requests == []
    assert world.plugin.requests == []


def test_a_question_of_exactly_2048_characters_is_accepted(world: World) -> None:
    question = ("refund " * 300)[:2_048]
    planner = ScriptedPlanner(ABSTAIN)

    result = run(world, planner, question=question)

    assert len(question) == 2_048
    assert len(planner.requests) == 1
    assert result.answer.abstained


def test_at_most_sixty_four_sources_can_be_planned(world: World) -> None:
    unregistered = [
        SourceRef(f"unregistered-{index}", "text/markdown", "rev-1")
        for index in range(64)
    ]
    planner = ScriptedPlanner(ABSTAIN)

    run(world, planner, sources=(HANDBOOK, *unregistered[:63]))

    assert len(planner.requests) == 1
    with pytest.raises(ValueError, match="at most 64 sources"):
        run(world, ScriptedPlanner(ABSTAIN), sources=(HANDBOOK, *unregistered))
    with pytest.raises(ValueError, match="at most 64 sources"):
        world.dispatcher.available_tools(TENANT, (HANDBOOK, *unregistered))


def test_source_ids_must_be_unique(world: World) -> None:
    planner = ScriptedPlanner(ABSTAIN)
    newer_handbook = SourceRef("handbook", "text/markdown", "rev-8")

    with pytest.raises(ValueError, match="unique"):
        run(world, planner, sources=(HANDBOOK, newer_handbook))

    assert planner.requests == []


@pytest.mark.parametrize("tenant_id", ["", "   ", None, 5])
def test_a_tenant_is_required(world: World, tenant_id: object) -> None:
    planner = ScriptedPlanner(ABSTAIN)

    with pytest.raises(ValueError, match="tenant_id"):
        run(world, planner, tenant_id=tenant_id)  # type: ignore[arg-type]

    assert planner.requests == []


def test_the_budget_limits_the_number_of_calls_before_any_of_them_run(
    world: World,
) -> None:
    planner = ScriptedPlanner(call_turn("a", "b", "c"))

    with pytest.raises(ReasoningBudgetExceededError, match="tool-call count"):
        run(world, planner, budget=ReasoningBudget(max_tool_calls=2))

    assert world.plugin.requests == []


@pytest.mark.parametrize(("max_depth", "executed"), [(0, []), (1, ["a"])])
def test_the_budget_limits_the_reasoning_depth(
    world: World, max_depth: int, executed: list[str]
) -> None:
    planner = ScriptedPlanner(call_turn("a"), call_turn("b"))

    with pytest.raises(ReasoningBudgetExceededError, match="depth"):
        run(world, planner, budget=ReasoningBudget(max_depth=max_depth))

    assert queries(world.plugin.requests) == executed


def test_a_planner_cannot_spend_more_tokens_than_the_budget(world: World) -> None:
    planner = ScriptedPlanner(PlannerTurn(tokens_used=11))

    with pytest.raises(ReasoningBudgetExceededError, match="token"):
        run(world, planner, budget=ReasoningBudget(max_tokens=10))

    assert planner.requests[0].remaining_tokens == 10


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_depth", -1),
        ("max_depth", 9),
        ("max_depth", True),
        ("max_depth", 2.5),
        ("max_depth", "3"),
        ("max_depth", None),
        ("max_tool_calls", 0),
        ("max_tool_calls", 33),
        ("max_tool_calls", True),
        ("max_tool_calls", None),
        ("max_tokens", 0),
        ("max_tokens", 128_001),
        ("max_tokens", False),
        ("maximum_results", 0),
        ("maximum_results", 101),
        ("maximum_results", True),
        ("timeout_seconds", 0),
        ("timeout_seconds", -1),
        ("timeout_seconds", 300.5),
        ("timeout_seconds", math.nan),
        ("timeout_seconds", math.inf),
        ("timeout_seconds", True),
        ("timeout_seconds", "30"),
        ("timeout_seconds", None),
    ],
)
def test_budget_values_outside_the_caps_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        ReasoningBudget(**{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "limits",
    [
        dict(
            max_depth=0,
            max_tool_calls=1,
            max_tokens=1,
            timeout_seconds=0.001,
            maximum_results=1,
        ),
        dict(
            max_depth=8,
            max_tool_calls=32,
            max_tokens=128_000,
            timeout_seconds=300,
            maximum_results=100,
        ),
    ],
    ids=["lowest", "highest"],
)
def test_budget_values_at_the_caps_are_accepted(limits: dict[str, float]) -> None:
    budget = ReasoningBudget(**limits)  # type: ignore[arg-type]

    assert budget.max_tool_calls == limits["max_tool_calls"]


# --- list_source_nodes --------------------------------------------------------------


def test_listing_source_nodes_is_scoped_to_one_source_and_keeps_insertion_order(
    catalog: SQLiteCatalog,
) -> None:
    catalog.create_source(HANDBOOK, tenant_id="tenant-a")
    catalog.create_source(LEDGER, tenant_id="tenant-b")
    catalog.upsert_nodes(
        [
            node(HANDBOOK, "h-root", "Zebra policy"),
            node(LEDGER, "l-root", "Ledger root"),
            node(HANDBOOK, "h-child", "Apple", parent_id="h-root"),
            node(HANDBOOK, "h-other", "Mango"),
            node(LEDGER, "l-child", "Ledger child", parent_id="l-root"),
        ]
    )

    assert [item.node_id for item in catalog.list_source_nodes("handbook")] == [
        "h-root",
        "h-child",
        "h-other",
    ]
    assert [item.node_id for item in catalog.list_source_nodes("ledger")] == [
        "l-root",
        "l-child",
    ]


def test_listing_source_nodes_only_returns_the_current_revision(
    catalog: SQLiteCatalog,
) -> None:
    newer = SourceRef("handbook", "text/markdown", "rev-8")
    catalog.create_source(HANDBOOK)
    catalog.upsert_nodes([node(HANDBOOK, "old-root", "Old")])

    catalog.update_source(newer)

    assert catalog.list_source_nodes("handbook") == ()
    catalog.upsert_nodes([node(newer, "new-root", "New")])
    assert [item.node_id for item in catalog.list_source_nodes("handbook")] == [
        "new-root"
    ]


def test_listing_source_nodes_honours_the_limit(catalog: SQLiteCatalog) -> None:
    catalog.create_source(HANDBOOK)
    catalog.upsert_nodes([node(HANDBOOK, f"n{index}", f"Node {index}") for index in range(5)])

    assert [item.node_id for item in catalog.list_source_nodes("handbook", limit=2)] == [
        "n0",
        "n1",
    ]
    assert len(catalog.list_source_nodes("handbook", limit=100_000)) == 5


@pytest.mark.parametrize("limit", [0, -1, 100_001, True, False, 2.0, "5", None])
def test_listing_source_nodes_rejects_invalid_limits(
    catalog: SQLiteCatalog, limit: object
) -> None:
    catalog.create_source(HANDBOOK)

    with pytest.raises(ValueError, match="limit"):
        catalog.list_source_nodes("handbook", limit=limit)  # type: ignore[arg-type]


def test_listing_nodes_of_an_unknown_source_returns_nothing(
    catalog: SQLiteCatalog,
) -> None:
    catalog.create_source(HANDBOOK)
    catalog.upsert_nodes([node(HANDBOOK, "h-root", "Root")])

    assert catalog.list_source_nodes("never-registered") == ()


def test_listing_source_nodes_serves_large_documents_in_one_call(
    catalog: SQLiteCatalog,
) -> None:
    catalog.create_source(HANDBOOK)
    catalog.upsert_nodes(
        [node(HANDBOOK, f"n{index:05d}", f"Section {index}") for index in range(1_200)]
    )

    default_page = catalog.list_source_nodes("handbook")
    everything = catalog.list_source_nodes("handbook", limit=5_000)

    assert len(default_page) == 1_000
    assert [item.node_id for item in everything] == [f"n{index:05d}" for index in range(1_200)]


# --- validate_tool_parameters ------------------------------------------------------


def wrap(leaf: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "type": "object",
        "properties": {"v": leaf},
        "required": ["v"],
        "additionalProperties": False,
    }


def check(leaf: Mapping[str, JsonValue], value: object) -> None:
    validate_tool_parameters(wrap(leaf), {"v": value})  # type: ignore[dict-item]


def rejection(leaf: Mapping[str, JsonValue], value: object) -> str:
    with pytest.raises(InvalidToolRequestError) as raised:
        check(leaf, value)
    return str(raised.value)


ACCEPTED_VALUES = [
    ({"type": "string"}, ""),
    ({"type": "string"}, "text"),
    ({"type": "integer"}, 0),
    ({"type": "integer"}, -7),
    ({"type": "number"}, 1.5),
    ({"type": "number"}, 3),
    ({"type": "boolean"}, True),
    ({"type": "boolean"}, False),
    ({"type": "array"}, []),
    ({"type": "array"}, [1, "a", None]),
    ({"type": "object"}, {}),
    ({"type": "object"}, {"a": 1}),
]
REJECTED_VALUES = [
    ({"type": "string"}, 1),
    ({"type": "string"}, None),
    ({"type": "string"}, ["a"]),
    ({"type": "string"}, True),
    ({"type": "integer"}, 1.5),
    ({"type": "integer"}, "3"),
    ({"type": "integer"}, True),
    ({"type": "integer"}, False),
    ({"type": "integer"}, None),
    ({"type": "number"}, "3"),
    ({"type": "number"}, True),
    ({"type": "number"}, None),
    ({"type": "boolean"}, 0),
    ({"type": "boolean"}, 1),
    ({"type": "boolean"}, "true"),
    ({"type": "boolean"}, None),
    ({"type": "array"}, "a"),
    ({"type": "array"}, {"a": 1}),
    ({"type": "array"}, None),
    ({"type": "object"}, []),
    ({"type": "object"}, "a"),
    ({"type": "object"}, None),
]


@pytest.mark.parametrize(
    ("leaf", "value"),
    [pytest.param(leaf, value, id=f"{leaf['type']}:{value!r}") for leaf, value in ACCEPTED_VALUES],
)
def test_values_of_the_declared_type_are_accepted(
    leaf: Mapping[str, JsonValue], value: object
) -> None:
    check(leaf, value)


@pytest.mark.parametrize(
    ("leaf", "value"),
    [pytest.param(leaf, value, id=f"{leaf['type']}:{value!r}") for leaf, value in REJECTED_VALUES],
)
def test_values_of_another_type_are_rejected_and_booleans_are_not_numbers(
    leaf: Mapping[str, JsonValue], value: object
) -> None:
    assert "$.v must match the declared" in rejection(leaf, value)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_numbers_are_rejected(value: float) -> None:
    assert "must be finite" in rejection({"type": "number"}, value)


@pytest.mark.parametrize(
    ("leaf", "value"),
    [
        ({"type": "string", "minLength": 2, "maxLength": 4}, "ab"),
        ({"type": "string", "minLength": 2, "maxLength": 4}, "abcd"),
        ({"type": "string", "minLength": 2, "maxLength": 4}, "éééé"),
        ({"type": "integer", "minimum": 1, "maximum": 5}, 1),
        ({"type": "integer", "minimum": 1, "maximum": 5}, 5),
        ({"type": "number", "minimum": 0.5}, 0.5),
        ({"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "integer"}}, [1]),
        ({"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "integer"}}, [1, 2, 3]),
        ({"type": "string", "enum": ["a", "b"]}, "b"),
        ({"type": "integer", "enum": [1, 2]}, 2),
        ({"type": "array"}, [1, "mixed", None]),
    ],
)
def test_values_on_the_limits_are_accepted(
    leaf: Mapping[str, JsonValue], value: object
) -> None:
    check(leaf, value)


@pytest.mark.parametrize(
    ("leaf", "value", "fragment"),
    [
        ({"type": "string", "minLength": 2, "maxLength": 4}, "a", "invalid string length"),
        ({"type": "string", "minLength": 2, "maxLength": 4}, "abcde", "invalid string length"),
        ({"type": "integer", "minimum": 1, "maximum": 5}, 0, "below its minimum"),
        ({"type": "integer", "minimum": 1, "maximum": 5}, 6, "exceeds its maximum"),
        ({"type": "number", "minimum": 0.5}, 0.4, "below its minimum"),
        ({"type": "array", "minItems": 1, "items": {"type": "integer"}}, [], "invalid number of items"),
        ({"type": "array", "maxItems": 3, "items": {"type": "integer"}}, [1, 2, 3, 4], "invalid number of items"),
        ({"type": "array", "items": {"type": "integer"}}, [1, "x"], "$.v[1] must match"),
        ({"type": "array", "items": {"type": "integer"}}, [True], "$.v[0] must match"),
        ({"type": "string", "enum": ["a", "b"]}, "c", "is not an allowed value"),
        ({"type": "integer", "enum": [1, 2]}, 3, "is not an allowed value"),
    ],
)
def test_values_beyond_the_limits_are_rejected(
    leaf: Mapping[str, JsonValue], value: object, fragment: str
) -> None:
    assert fragment in rejection(leaf, value)


@pytest.mark.parametrize(
    "keyword",
    [
        ("pattern", "^a$"),
        ("format", "date"),
        ("$ref", "#/definitions/other"),
        ("oneOf", [{"type": "string"}]),
        ("anyOf", [{"type": "string"}]),
        ("allOf", [{"type": "string"}]),
        ("not", {"type": "integer"}),
        ("const", "a"),
        ("default", "a"),
        ("uniqueItems", True),
        ("multipleOf", 2),
        ("exclusiveMinimum", 0),
        ("patternProperties", {"^a": {"type": "string"}}),
        ("minProperties", 1),
        ("additionalItems", False),
        ("if", {"type": "string"}),
    ],
    ids=lambda keyword: keyword[0],
)
def test_unsupported_schema_keywords_fail_closed(keyword: tuple[str, JsonValue]) -> None:
    name, value = keyword

    nested = rejection({"type": "string", name: value}, "anything")
    with pytest.raises(InvalidToolRequestError, match="unsupported tool schema keywords") as root:
        validate_tool_parameters({"type": "object", name: value}, {})

    assert "unsupported tool schema keywords at $.v" in nested
    assert name in nested
    assert name in str(root.value)


def test_annotation_keywords_are_accepted_anywhere() -> None:
    schema: dict[str, JsonValue] = {
        "type": "object",
        "title": "Search",
        "description": "Search one source",
        "properties": {
            "query": {"type": "string", "title": "Query", "description": "Text to find"}
        },
        "required": ["query"],
    }

    validate_tool_parameters(schema, {"query": "refunds"})


def test_required_fields_are_enforced_and_named() -> None:
    schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        "required": ["a", "b"],
    }

    with pytest.raises(InvalidToolRequestError, match="missing required fields") as raised:
        validate_tool_parameters(schema, {"a": "x"})

    assert "'b'" in str(raised.value)
    assert "'a'" not in str(raised.value)


def test_additional_properties_are_allowed_unless_the_schema_forbids_them() -> None:
    open_schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
    }
    explicit: dict[str, JsonValue] = {**open_schema, "additionalProperties": True}
    closed: dict[str, JsonValue] = {**open_schema, "additionalProperties": False}
    typed: dict[str, JsonValue] = {
        **open_schema,
        "additionalProperties": {"type": "integer"},
    }
    broken: dict[str, JsonValue] = {**open_schema, "additionalProperties": "yes"}
    extra = {"a": "x", "other": 5}

    validate_tool_parameters(open_schema, extra)
    validate_tool_parameters(explicit, extra)
    validate_tool_parameters(closed, {"a": "x"})
    validate_tool_parameters(typed, extra)
    with pytest.raises(InvalidToolRequestError, match="is not an allowed field") as raised:
        validate_tool_parameters(closed, extra)
    assert "$.other" in str(raised.value)
    with pytest.raises(InvalidToolRequestError, match="declared 'integer' type"):
        validate_tool_parameters(typed, {"a": "x", "other": "five"})
    with pytest.raises(InvalidToolRequestError, match="invalid additionalProperties"):
        validate_tool_parameters(broken, extra)


NESTED_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "filters": {
            "type": "object",
            "properties": {
                "dates": {"type": "array", "items": {"type": "string", "minLength": 2}}
            },
            "required": ["dates"],
            "additionalProperties": False,
        }
    },
    "required": ["filters"],
    "additionalProperties": False,
}


def test_nested_objects_and_arrays_are_validated_to_the_leaves() -> None:
    validate_tool_parameters(NESTED_SCHEMA, {"filters": {"dates": ["ab", "cd"]}})
    validate_tool_parameters(NESTED_SCHEMA, {"filters": {"dates": []}})


@pytest.mark.parametrize(
    ("parameters", "fragment"),
    [
        ({"filters": {"dates": ["ab", "c"]}}, "$.filters.dates[1] has an invalid string length"),
        ({"filters": {"dates": ["ab", 5]}}, "$.filters.dates[1] must match the declared 'string' type"),
        ({"filters": {}}, "$.filters is missing required fields"),
        ({"filters": {"dates": [], "extra": 1}}, "$.filters.extra is not an allowed field"),
        ({"filters": []}, "$.filters must match the declared 'object' type"),
        ({}, "$ is missing required fields"),
        ({"other": 1, "filters": {"dates": []}}, "$.other is not an allowed field"),
    ],
)
def test_validation_errors_point_at_the_offending_path(
    parameters: dict[str, JsonValue], fragment: str
) -> None:
    with pytest.raises(InvalidToolRequestError) as raised:
        validate_tool_parameters(NESTED_SCHEMA, parameters)

    assert fragment in str(raised.value)


@pytest.mark.parametrize("parameters", [["query"], "query", None, 5])
def test_parameters_must_be_an_object(parameters: object) -> None:
    with pytest.raises(InvalidToolRequestError, match="declared 'object' type"):
        validate_tool_parameters(SEARCH_SCHEMA, parameters)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("leaf", "value"),
    [
        ({"type": "integer"}, SECRET),
        ({"type": "boolean"}, SECRET),
        ({"type": "string", "enum": ["a", "b"]}, SECRET),
        ({"type": "string", "maxLength": 5}, SECRET),
        ({"type": "array", "items": {"type": "integer"}}, [SECRET]),
        ({"type": "object", "additionalProperties": {"type": "integer"}}, {"k": SECRET}),
        ({"type": "object", "additionalProperties": False}, {"k": SECRET}),
    ],
)
def test_validation_errors_never_echo_parameter_values(
    leaf: Mapping[str, JsonValue], value: object
) -> None:
    assert SECRET not in rejection(leaf, value)


def nested_object(levels: int) -> tuple[dict[str, JsonValue], JsonValue]:
    schema: dict[str, JsonValue] = {"type": "string"}
    value: JsonValue = "leaf"
    for _ in range(levels):
        schema = {
            "type": "object",
            "properties": {"a": schema},
            "required": ["a"],
            "additionalProperties": False,
        }
        value = {"a": value}
    return schema, value


def test_schemas_nested_beyond_sixteen_levels_are_rejected() -> None:
    schema, value = nested_object(16)
    validate_tool_parameters(schema, value)  # type: ignore[arg-type]

    deeper_schema, deeper_value = nested_object(17)
    with pytest.raises(InvalidToolRequestError, match="invalid tool schema"):
        validate_tool_parameters(deeper_schema, deeper_value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("schema", "parameters", "fragment"),
    [
        ({}, {}, "declared None type"),
        ({"type": "null"}, {}, "declared 'null' type"),
        ({"type": ["object"]}, {}, "declared"),
        ({"type": "object", "properties": ["a"]}, {}, "invalid object schema"),
        ({"type": "object", "required": "a"}, {}, "invalid object schema"),
        ({"type": "object", "properties": {"a": "string"}}, {"a": "x"}, "invalid property schema"),
        (wrap({"type": "string", "enum": "abc"}), {"v": "x"}, "invalid enum"),
        (wrap({"type": "string", "minLength": -1}), {"v": "x"}, "invalid minLength"),
        (wrap({"type": "string", "minLength": True}), {"v": "x"}, "invalid minLength"),
        (wrap({"type": "string", "minLength": 5, "maxLength": 2}), {"v": "abc"}, "invalid string limits"),
        (wrap({"type": "array", "minItems": 3, "maxItems": 1}), {"v": [1, 2]}, "invalid item limits"),
        (wrap({"type": "array", "items": []}), {"v": [1]}, "invalid array schema"),
        (wrap({"type": "integer", "minimum": 5, "maximum": 1}), {"v": 3}, "invalid numeric limits"),
        (wrap({"type": "integer", "minimum": "a"}), {"v": 3}, "invalid numeric limits"),
        (wrap({"type": "integer", "minimum": True}), {"v": 3}, "invalid numeric limits"),
        (wrap({"type": "number", "maximum": math.inf}), {"v": 3}, "invalid numeric limits"),
    ],
)
def test_malformed_schemas_are_reported_instead_of_trusted(
    schema: dict[str, JsonValue], parameters: dict[str, JsonValue], fragment: str
) -> None:
    with pytest.raises(InvalidToolRequestError) as raised:
        validate_tool_parameters(schema, parameters)

    assert fragment in str(raised.value)


# --- tool schemas are validated when tools are discovered ------------------------------


def oversized_schema() -> dict[str, JsonValue]:
    return {
        "type": "object",
        "properties": {"q": {"type": "string", "description": "x" * 40_000}},
    }


INVALID_TOOL_SCHEMAS: list[tuple[str, dict[str, JsonValue]]] = [
    ("pattern", wrap({"type": "string", "pattern": "^a"})),
    ("keyword-of-another-type", wrap({"type": "string", "minimum": 1})),
    ("missing-type", {"properties": {}}),
    ("unknown-type", {"type": "null"}),
    ("empty-enum", wrap({"type": "string", "enum": []})),
    (
        "undeclared-required",
        {"type": "object", "properties": {}, "required": ["q"], "additionalProperties": False},
    ),
    (
        "duplicate-required",
        {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q", "q"]},
    ),
    ("inverted-length-limits", wrap({"type": "string", "minLength": 5, "maxLength": 2})),
    ("negative-length", wrap({"type": "string", "minLength": -1})),
    ("boolean-item-limit", wrap({"type": "array", "minItems": True})),
    ("non-string-title", {"type": "object", "title": 5}),
    (
        "too-many-properties",
        {"type": "object", "properties": {f"p{index}": {"type": "string"} for index in range(129)}},
    ),
    ("oversized", oversized_schema()),
    ("too-deep", nested_object(18)[0]),
]


@pytest.mark.parametrize(
    "schema",
    [pytest.param(schema, id=name) for name, schema in INVALID_TOOL_SCHEMAS],
)
def test_a_plugin_with_an_invalid_tool_schema_is_skipped_and_never_dispatched(
    world: World, schema: dict[str, JsonValue], caplog: pytest.LogCaptureFixture
) -> None:
    world.plugin.schema = schema

    with caplog.at_level(logging.WARNING, logger="dataindex.reasoning"):
        advertised = world.dispatcher.available_tools(TENANT, (HANDBOOK,))

    assert advertised == ()
    assert [record.levelno for record in caplog.records] == [logging.WARNING]
    with pytest.raises(ToolNotAvailableError):
        dispatch(world, search("refunds"))
    assert world.plugin.requests == []


def test_a_valid_tool_schema_is_advertised(world: World) -> None:
    (tool,) = world.dispatcher.available_tools(TENANT, (HANDBOOK,))

    assert (tool.plugin_id, tool.source, tool.spec.tool_id) == (
        "docs",
        HANDBOOK,
        "documents.search",
    )
