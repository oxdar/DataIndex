"""Bounded, source-grounded reasoning and centralized extension dispatch."""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from dataindex.catalog import SQLiteCatalog
from dataindex.entitlements import (
    EntitlementDecision,
    EntitlementProvider,
    EntitlementReason,
)
from dataindex.exceptions import (
    DataIndexError,
    EntitlementDeniedError,
    InvalidCitationError,
    InvalidPlannerOutputError,
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
    DataIndexExtension,
    EvidenceRecord,
    ExtensionManifest,
    HealthStatus,
    JsonValue,
    Node,
    RetrievalContext,
    RetrievalToolSpec,
    SourceAwareToolProvider,
    SourceRef,
    ToolRequest,
)
from dataindex.registry import ExtensionRegistry

MAX_REASONING_SOURCES = 64
MAX_HIERARCHY_NODES_PER_SOURCE = 256
MAX_TOOL_CALLS = 32
MAX_REASONING_DEPTH = 8
MAX_REASONING_TOKENS = 128_000
MAX_REASONING_TIMEOUT_SECONDS = 300.0
MAX_EVIDENCE_BYTES = 256 * 1024
LOGGER = logging.getLogger(__name__)
_TOKEN = re.compile(r"[a-z0-9]{2,}")
_STOP_WORDS = frozenset(
    {
        "about",
        "after",
        "and",
        "are",
        "can",
        "for",
        "from",
        "how",
        "into",
        "that",
        "the",
        "their",
        "this",
        "what",
        "when",
        "where",
        "which",
        "with",
    }
)


class SourceAuthorizer(Protocol):
    def is_authorized(
        self, tenant_id: str, source: SourceRef, capability: str
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class ReasoningBudget:
    max_depth: int = 3
    max_tool_calls: int = 8
    max_tokens: int = 8_000
    timeout_seconds: float = 30.0
    maximum_results: int = 100

    def __post_init__(self) -> None:
        integer_limits = (
            ("max_depth", self.max_depth, 0, MAX_REASONING_DEPTH),
            ("max_tool_calls", self.max_tool_calls, 1, MAX_TOOL_CALLS),
            ("max_tokens", self.max_tokens, 1, MAX_REASONING_TOKENS),
            ("maximum_results", self.maximum_results, 1, 100),
        )
        for name, value, minimum, maximum in integer_limits:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= MAX_REASONING_TIMEOUT_SECONDS
        ):
            raise ValueError(
                f"timeout_seconds must be greater than 0 and at most "
                f"{MAX_REASONING_TIMEOUT_SECONDS}"
            )


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    source: SourceRef
    score: int
    matched_titles: tuple[str, ...]


class HierarchicalSourcePlanner:
    """Rank authorized sources using bounded traversal of catalog metadata."""

    def __init__(self, catalog: SQLiteCatalog) -> None:
        self._catalog = catalog

    def select(
        self,
        question: str,
        sources: Sequence[SourceRef],
        *,
        limit: int = MAX_REASONING_SOURCES,
        require_match: bool = True,
    ) -> tuple[SourceCandidate, ...]:
        """Rank sources by title overlap with the question.

        With ``require_match=False`` every current source is kept, in score then
        ID order, so that an LLM planner can decide relevance itself. Unmatched
        sources carry a zero score and no matched titles.
        """
        if not isinstance(question, str) or not question.strip() or len(question) > 2048:
            raise ValueError("question must contain 1 to 2048 characters")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_REASONING_SOURCES
        ):
            raise ValueError(f"limit must be between 1 and {MAX_REASONING_SOURCES}")
        terms = _question_terms(question)
        if not terms and require_match:
            return ()
        candidates: list[SourceCandidate] = []
        seen_sources: set[str] = set()
        for source in sources:
            if source.source_id in seen_sources:
                raise ValueError(f"duplicate source ID {source.source_id!r}")
            seen_sources.add(source.source_id)
            if len(seen_sources) > MAX_REASONING_SOURCES:
                raise ValueError(
                    f"at most {MAX_REASONING_SOURCES} sources may be planned"
                )
            current = self._catalog.get_source(source.source_id)
            if current != source:
                raise StaleSourceVersionError(
                    f"source {source.source_id!r} is not at the requested revision"
                )
            nodes = self._hierarchy(source) if terms else ()
            scored = [
                (self._node_score(node.title, node.summary, terms), node.title)
                for node in nodes
            ]
            scored = [(score, title) for score, title in scored if score > 0]
            if not scored:
                if not require_match:
                    candidates.append(
                        SourceCandidate(source=source, score=0, matched_titles=())
                    )
                continue
            scored.sort(key=lambda item: (-item[0], item[1]))
            candidates.append(
                SourceCandidate(
                    source=source,
                    score=sum(score for score, _ in scored[:4]),
                    matched_titles=tuple(title for _, title in scored[:4]),
                )
            )
        candidates.sort(key=lambda item: (-item.score, item.source.source_id))
        return tuple(candidates[:limit])

    def _hierarchy(self, source: SourceRef) -> tuple[Node, ...]:
        nodes: list[Node] = []
        pending: list[str | None] = [None]
        visited: set[str] = set()
        while pending and len(nodes) < MAX_HIERARCHY_NODES_PER_SOURCE:
            parent_id = pending.pop(0)
            remaining = MAX_HIERARCHY_NODES_PER_SOURCE - len(nodes)
            children = self._catalog.list_children(
                parent_id,
                source_id=source.source_id,
                limit=min(remaining, 100),
            )
            for node in children:
                if node.node_id in visited:
                    continue
                visited.add(node.node_id)
                nodes.append(node)
                if len(nodes) < MAX_HIERARCHY_NODES_PER_SOURCE:
                    pending.append(node.node_id)
        return tuple(nodes)

    @staticmethod
    def _node_score(title: str, summary: str | None, terms: frozenset[str]) -> int:
        title_terms = set(_TOKEN.findall(title.casefold()))
        summary_terms = set(_TOKEN.findall((summary or "").casefold()))
        return 3 * len(title_terms & terms) + len(summary_terms & terms)


def _question_terms(question: str) -> frozenset[str]:
    return frozenset(
        term
        for term in _TOKEN.findall(question.casefold())
        if term not in _STOP_WORDS
    )


@dataclass(frozen=True, slots=True)
class ToolCandidate:
    plugin_id: str
    source: SourceRef
    spec: RetrievalToolSpec
    source_score: int
    matched_titles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlannedToolCall:
    plugin_id: str
    source: SourceRef
    request: ToolRequest


@dataclass(frozen=True, slots=True)
class GroundedClaim:
    text: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.text, str)
            or not self.text.strip()
            or len(self.text) > 8192
        ):
            raise ValueError("claim text must contain 1 to 8192 characters")
        if not self.evidence_ids or not all(
            isinstance(item, str) and item.strip() for item in self.evidence_ids
        ):
            raise ValueError("each claim must cite at least one evidence ID")
        if len(self.evidence_ids) > 16:
            raise ValueError("a claim may cite at most 16 evidence IDs")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("claim evidence IDs must be unique")


@dataclass(frozen=True, slots=True)
class PlannerTurn:
    tool_calls: tuple[PlannedToolCall, ...] = ()
    claims: tuple[GroundedClaim, ...] = ()
    tokens_used: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.tool_calls, tuple) or not all(
            isinstance(call, PlannedToolCall) for call in self.tool_calls
        ):
            raise ValueError("tool_calls must contain PlannedToolCall values")
        if not isinstance(self.claims, tuple) or not all(
            isinstance(claim, GroundedClaim) for claim in self.claims
        ):
            raise ValueError("claims must contain GroundedClaim values")
        if self.tool_calls and self.claims:
            raise ValueError("a planner turn cannot both call tools and make claims")
        if (
            isinstance(self.tokens_used, bool)
            or not isinstance(self.tokens_used, int)
            or self.tokens_used < 0
        ):
            raise ValueError("tokens_used must be a non-negative integer")


class LLMPlanner(Protocol):
    """Provider-neutral adapter; credentials and SDKs stay outside core."""

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
    ) -> PlannerTurn: ...


@dataclass(frozen=True, slots=True)
class GroundedAnswer:
    claims: tuple[GroundedClaim, ...]
    evidence: tuple[EvidenceRecord, ...]
    abstained: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ReasoningResult:
    answer: GroundedAnswer
    selected_sources: tuple[SourceRef, ...]
    tool_calls: tuple[PlannedToolCall, ...]
    depth: int
    tokens_used: int


class GroundedAnswerComposer:
    def compose(
        self,
        claims: tuple[GroundedClaim, ...],
        evidence: tuple[EvidenceRecord, ...],
        *,
        abstention_reason: str | None = None,
    ) -> GroundedAnswer:
        evidence_by_id = {item.execution_id: item for item in evidence}
        if len(evidence_by_id) != len(evidence):
            raise InvalidToolEvidenceError("tool evidence IDs must be unique")
        for claim in claims:
            missing = set(claim.evidence_ids) - evidence_by_id.keys()
            if missing:
                raise InvalidCitationError(
                    f"claim cites unknown evidence IDs: {sorted(missing)}"
                )
        if not claims:
            return GroundedAnswer(
                claims=(),
                evidence=evidence,
                abstained=True,
                reason=abstention_reason
                or "No supported claims were produced from authorized evidence.",
            )
        return GroundedAnswer(
            claims=claims,
            evidence=evidence,
            abstained=False,
        )


class ToolDispatcher:
    """Recheck plugin trust, tenant access, entitlement and evidence per call."""

    def __init__(
        self,
        registry: ExtensionRegistry,
        catalog: SQLiteCatalog,
        source_authorizer: SourceAuthorizer,
        entitlement_provider: EntitlementProvider | None = None,
        *,
        deployment: str,
    ) -> None:
        if not isinstance(deployment, str) or not deployment.strip():
            raise ValueError("deployment must be a non-empty string")
        self._registry = registry
        self._catalog = catalog
        self._source_authorizer = source_authorizer
        self._entitlement_provider = entitlement_provider
        self._deployment = deployment

    def available_tools(
        self, tenant_id: str, sources: Sequence[SourceRef]
    ) -> tuple[ToolCandidate, ...]:
        _validate_tenant(tenant_id)
        if len(sources) > MAX_REASONING_SOURCES:
            raise ValueError(
                f"at most {MAX_REASONING_SOURCES} sources may be planned"
            )
        result: list[ToolCandidate] = []
        for source in sources:
            source_row = self._catalog.get_source(source.source_id)
            if source_row is None:
                continue
            if self._catalog.get_source_tenant(source.source_id) != tenant_id:
                continue
            if not self._source_authorizer.is_authorized(
                tenant_id, source, "source.access"
            ):
                continue
            if source_row != source:
                raise StaleSourceVersionError(
                    f"source {source.source_id!r} is not at the requested revision"
                )
            for plugin_id in sorted(self._registry.loaded_extensions):
                try:
                    extension, manifest = self._trusted_extension(plugin_id)
                    if source.source_type not in manifest.source_types:
                        continue
                    if not extension.probe(source).supported:
                        continue
                    specs = self._source_tools(extension, source)
                    for spec in specs:
                        self._validate_spec(plugin_id, spec, manifest)
                except ToolNotAvailableError as error:
                    LOGGER.warning(
                        "Skipping plugin %s during tool discovery: %s",
                        plugin_id,
                        error,
                    )
                    continue
                for spec in specs:
                    if not self._source_authorizer.is_authorized(
                        tenant_id, source, spec.capability
                    ):
                        continue
                    if not self._has_entitlement(
                        tenant_id, spec, manifest, deny=False
                    ):
                        continue
                    result.append(
                        ToolCandidate(
                            plugin_id=plugin_id,
                            source=source,
                            spec=spec,
                            source_score=0,
                            matched_titles=(),
                        )
                    )
        return tuple(result)

    def dispatch(
        self,
        tenant_id: str,
        call: PlannedToolCall,
        *,
        deadline_monotonic: float,
        maximum_results: int,
    ) -> EvidenceRecord:
        _check_deadline(deadline_monotonic)
        _validate_tenant(tenant_id)
        if (
            isinstance(maximum_results, bool)
            or not isinstance(maximum_results, int)
            or maximum_results < 1
        ):
            raise ValueError("maximum_results must be positive")
        current = self._catalog.get_source(call.source.source_id)
        if current is None:
            raise SourceAuthorizationError("source is not registered")
        if self._catalog.get_source_tenant(call.source.source_id) != tenant_id:
            raise SourceAuthorizationError("source does not belong to this tenant")
        if not self._source_authorizer.is_authorized(
            tenant_id, call.source, "source.access"
        ):
            raise SourceAuthorizationError("tenant is not authorized for this source")
        if current != call.source:
            raise StaleSourceVersionError(
                f"source {call.source.source_id!r} is not at the requested revision"
            )
        extension, manifest = self._trusted_extension(call.plugin_id)
        if call.source.source_type not in manifest.source_types:
            raise ToolNotAvailableError("plugin does not support this source type")
        if not extension.probe(call.source).supported:
            raise ToolNotAvailableError("plugin does not support this source")
        spec = next(
            (
                item
                for item in self._source_tools(extension, call.source)
                if item.tool_id == call.request.tool_id
            ),
            None,
        )
        if spec is None:
            raise ToolNotAvailableError("requested tool is not available for source")
        self._validate_spec(call.plugin_id, spec, manifest)
        if not self._source_authorizer.is_authorized(
            tenant_id, call.source, spec.capability
        ):
            raise SourceAuthorizationError(
                f"tenant is not authorized for capability {spec.capability!r}"
            )
        self._has_entitlement(
            tenant_id, spec, manifest, deny=True
        )
        parameters = call.request.parameters
        if (
            parameters.get("source_id") != call.source.source_id
            or parameters.get("source_version") != call.source.version
        ):
            raise InvalidToolRequestError(
                "tool request source_id and source_version must match the selected source"
            )
        _validate_json_schema(spec.input_schema, parameters)
        _check_deadline(deadline_monotonic)
        try:
            evidence = extension.run_tool(
                call.request,
                RetrievalContext(
                    deadline_monotonic=deadline_monotonic,
                    maximum_results=min(maximum_results, spec.maximum_results),
                ),
            )
        except (DataIndexError, MemoryError, RecursionError):
            raise
        except Exception as error:
            LOGGER.warning(
                "Tool %s failed with %s", call.request.tool_id, type(error).__name__
            )
            raise ToolExecutionError(
                f"tool {call.request.tool_id!r} failed with {type(error).__name__}",
                error_type=type(error).__name__,
            ) from error
        _check_deadline(deadline_monotonic)
        self._validate_evidence(call, evidence)
        return evidence

    def _trusted_extension(
        self, plugin_id: str
    ) -> tuple[DataIndexExtension, ExtensionManifest]:
        status = next(
            (
                item
                for item in self._registry.statuses
                if item.plugin_id == plugin_id
            ),
            None,
        )
        extension = self._registry.loaded_extensions.get(plugin_id)
        if (
            status is None
            or extension is None
            or not status.compatible
            or not status.trusted
            or not status.enabled
            or status.healthy is not True
        ):
            raise ToolNotAvailableError(
                f"plugin {plugin_id!r} is not trusted, enabled and healthy"
            )
        manifest = extension.manifest()
        if (
            manifest.plugin_id != plugin_id
            or manifest.distribution != status.distribution
            or manifest.plugin_version != status.distribution_version
        ):
            raise ToolNotAvailableError("plugin runtime manifest changed after loading")
        if extension.health().status is not HealthStatus.HEALTHY:
            raise ToolNotAvailableError(f"plugin {plugin_id!r} is not healthy")
        return extension, manifest

    @staticmethod
    def _source_tools(
        extension: DataIndexExtension, source: SourceRef
    ) -> tuple[RetrievalToolSpec, ...]:
        provided = (
            extension.tools_for_source(source)
            if isinstance(extension, SourceAwareToolProvider)
            else extension.tools()
        )
        if not isinstance(provided, Sequence) or isinstance(provided, (str, bytes)):
            raise ToolNotAvailableError("plugin returned invalid tool specifications")
        if not all(isinstance(item, RetrievalToolSpec) for item in provided):
            raise ToolNotAvailableError("plugin returned an invalid tool specification")
        specifications = tuple(provided)
        for spec in specifications:
            _validate_schema_definition(spec.input_schema)
        return specifications

    @staticmethod
    def _validate_spec(
        plugin_id: str, spec: RetrievalToolSpec, manifest: ExtensionManifest
    ) -> None:
        expected_entitlement = manifest.capability_entitlements.get(spec.capability)
        if spec.capability not in manifest.capabilities:
            raise ToolNotAvailableError(
                f"plugin {plugin_id!r} exposes undeclared capability "
                f"{spec.capability!r}"
            )
        if (
            (manifest.edition == "commercial" and not expected_entitlement)
            or spec.entitlement_capability
            not in {None, expected_entitlement}
            or (manifest.edition == "free" and expected_entitlement is not None)
        ):
            raise ToolNotAvailableError(
                f"plugin {plugin_id!r} has inconsistent entitlement metadata"
            )

    def _has_entitlement(
        self,
        tenant_id: str,
        spec: RetrievalToolSpec,
        manifest: ExtensionManifest,
        *,
        deny: bool,
    ) -> bool:
        entitlement = manifest.capability_entitlements.get(spec.capability)
        if entitlement is None:
            return True
        decision = (
            self._entitlement_provider.check(
                tenant_id, entitlement, self._deployment
            )
            if self._entitlement_provider is not None
            else EntitlementDecision(
                False, EntitlementReason.PROVIDER_UNAVAILABLE
            )
        )
        if not isinstance(decision, EntitlementDecision):
            raise TypeError(
                "entitlement providers must return an EntitlementDecision"
            )
        if not decision.granted:
            LOGGER.warning(
                "Commercial tool entitlement denied for capability %s: %s",
                spec.capability,
                decision.reason.value,
            )
            if deny:
                raise EntitlementDeniedError(
                    f"entitlement {entitlement!r} is unavailable for "
                    f"{spec.capability!r}: {decision.reason.value}",
                    reason_code=decision.reason.value,
                )
            return False
        return True

    def _validate_evidence(
        self, call: PlannedToolCall, evidence: EvidenceRecord
    ) -> None:
        if not isinstance(evidence, EvidenceRecord):
            raise InvalidToolEvidenceError("tool returned a non-evidence result")
        if (
            evidence.source_id != call.source.source_id
            or evidence.source_version != call.source.version
            or evidence.operation != call.request.tool_id
            or not evidence.execution_id.strip()
        ):
            raise InvalidToolEvidenceError(
                "tool evidence source, version or operation does not match the call"
            )
        current = self._catalog.get_source(evidence.source_id)
        if current != call.source:
            raise StaleSourceVersionError(
                "source changed while the retrieval tool was running"
            )
        if evidence.node_id is not None:
            node = self._catalog.get_node(evidence.node_id)
            if (
                node is None
                or node.source_id != evidence.source_id
                or node.source_version != evidence.source_version
                or node.locator != evidence.locator
            ):
                raise InvalidToolEvidenceError(
                    "evidence locator does not match its catalog node"
                )
        try:
            encoded = json.dumps(
                {
                    "execution_id": evidence.execution_id,
                    "source_id": evidence.source_id,
                    "source_version": evidence.source_version,
                    "operation": evidence.operation,
                    "locator": dict(evidence.locator.value),
                    "value": evidence.value,
                },
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise InvalidToolEvidenceError(
                "tool evidence is not JSON serializable"
            ) from error
        if len(encoded) > MAX_EVIDENCE_BYTES:
            raise ToolEvidenceTooLargeError(
                f"tool evidence exceeds {MAX_EVIDENCE_BYTES} bytes"
            )


class ReasoningEngine:
    def __init__(
        self,
        dispatcher: ToolDispatcher,
        catalog: SQLiteCatalog,
        *,
        source_planner: HierarchicalSourcePlanner | None = None,
        answer_composer: GroundedAnswerComposer | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._source_planner = source_planner or HierarchicalSourcePlanner(catalog)
        self._answer_composer = answer_composer or GroundedAnswerComposer()

    def run_deterministic(
        self,
        *,
        tenant_id: str,
        question: str,
        sources: Sequence[SourceRef],
        tool_calls: Sequence[PlannedToolCall],
        claim_builder: Callable[
            [tuple[EvidenceRecord, ...]], tuple[GroundedClaim, ...]
        ]
        | None = None,
        budget: ReasoningBudget = ReasoningBudget(),
    ) -> ReasoningResult:
        deadline = time.monotonic() + budget.timeout_seconds
        selected, candidates = self._prepare(
            tenant_id, question, sources
        )
        calls = tuple(tool_calls)
        if len(calls) > budget.max_tool_calls:
            raise ReasoningBudgetExceededError("maximum tool-call count exceeded")
        evidence: list[EvidenceRecord] = []
        for call in calls:
            _check_deadline(deadline)
            self._validate_candidate(call, candidates)
        for call in calls:
            evidence.append(
                self._dispatcher.dispatch(
                    tenant_id,
                    call,
                    deadline_monotonic=deadline,
                    maximum_results=budget.maximum_results,
                )
            )
        evidence_tuple = tuple(evidence)
        claims = () if claim_builder is None else claim_builder(evidence_tuple)
        if not isinstance(claims, tuple) or not all(
            isinstance(claim, GroundedClaim) for claim in claims
        ):
            raise InvalidPlannerOutputError(
                "claim_builder must return a tuple of GroundedClaim values"
            )
        answer = self._answer_composer.compose(claims, evidence_tuple)
        return ReasoningResult(
            answer=answer,
            selected_sources=tuple(item.source for item in selected),
            tool_calls=calls,
            depth=1 if calls else 0,
            tokens_used=0,
        )

    def run_with_planner(
        self,
        *,
        tenant_id: str,
        question: str,
        sources: Sequence[SourceRef],
        planner: LLMPlanner,
        budget: ReasoningBudget = ReasoningBudget(),
        require_source_match: bool = True,
        tolerate_tool_failures: bool = False,
    ) -> ReasoningResult:
        """Run a planner loop; the opt-in flags exist for conversational planners.

        With ``tolerate_tool_failures`` a tool that raises, or returns too much
        evidence, costs one tool call and is reported to the planner through its
        optional ``report_tool_failure(call, error_type)`` hook instead of ending
        the run. Authorization, entitlement, provenance and budget errors always
        end the run.
        """
        deadline = time.monotonic() + budget.timeout_seconds
        selected, candidates = self._prepare(
            tenant_id,
            question,
            sources,
            require_source_match=require_source_match,
        )
        if not candidates:
            answer = self._answer_composer.compose(
                (), (), abstention_reason="No authorized source matched the question."
            )
            return ReasoningResult(answer, (), (), 0, 0)
        evidence: list[EvidenceRecord] = []
        executed_calls: list[PlannedToolCall] = []
        tokens_used = 0
        depth = 0
        while True:
            _check_deadline(deadline)
            turn = planner.plan(
                question=question,
                tools=candidates,
                evidence=tuple(evidence),
                depth=depth,
                remaining_tool_calls=budget.max_tool_calls - len(executed_calls),
                remaining_tokens=budget.max_tokens - tokens_used,
                deadline_monotonic=deadline,
            )
            _check_deadline(deadline)
            if not isinstance(turn, PlannerTurn):
                raise InvalidPlannerOutputError("planner must return a PlannerTurn")
            if turn.tokens_used > budget.max_tokens - tokens_used:
                raise ReasoningBudgetExceededError("maximum token budget exceeded")
            tokens_used += turn.tokens_used
            if turn.tool_calls:
                if depth >= budget.max_depth:
                    raise ReasoningBudgetExceededError("maximum reasoning depth exceeded")
                if len(executed_calls) + len(turn.tool_calls) > budget.max_tool_calls:
                    raise ReasoningBudgetExceededError("maximum tool-call count exceeded")
                for call in turn.tool_calls:
                    self._validate_candidate(call, candidates)
                for call in turn.tool_calls:
                    _check_deadline(deadline)
                    try:
                        record = self._dispatcher.dispatch(
                            tenant_id,
                            call,
                            deadline_monotonic=deadline,
                            maximum_results=budget.maximum_results,
                        )
                    except (ToolExecutionError, ToolEvidenceTooLargeError) as error:
                        if not tolerate_tool_failures:
                            raise
                        _report_tool_failure(planner, call, error)
                    else:
                        evidence.append(record)
                    executed_calls.append(call)
                depth += 1
                continue
            answer = self._answer_composer.compose(
                turn.claims,
                tuple(evidence),
                abstention_reason="No source evidence supports an answer.",
            )
            return ReasoningResult(
                answer=answer,
                selected_sources=tuple(item.source for item in selected),
                tool_calls=tuple(executed_calls),
                depth=depth,
                tokens_used=tokens_used,
            )

    def _prepare(
        self,
        tenant_id: str,
        question: str,
        sources: Sequence[SourceRef],
        *,
        require_source_match: bool = True,
    ) -> tuple[tuple[SourceCandidate, ...], tuple[ToolCandidate, ...]]:
        _validate_tenant(tenant_id)
        if not isinstance(question, str) or not question.strip() or len(question) > 2048:
            raise ValueError("question must contain 1 to 2048 characters")
        if len(sources) > MAX_REASONING_SOURCES:
            raise ValueError(
                f"at most {MAX_REASONING_SOURCES} sources may be planned"
            )
        if len({source.source_id for source in sources}) != len(sources):
            raise ValueError("source IDs must be unique")
        tools = self._dispatcher.available_tools(tenant_id, sources)
        accessible_sources = tuple(
            sorted(
                {tool.source for tool in tools},
                key=lambda source: source.source_id,
            )
        )
        if require_source_match:
            selected = self._source_planner.select(question, accessible_sources)
        else:
            selected = self._source_planner.select(
                question, accessible_sources, require_match=False
            )
        scores = {item.source: item for item in selected}
        candidates = tuple(
            ToolCandidate(
                plugin_id=tool.plugin_id,
                source=tool.source,
                spec=tool.spec,
                source_score=scores[tool.source].score,
                matched_titles=scores[tool.source].matched_titles,
            )
            for tool in tools
            if tool.source in scores
        )
        return selected, candidates

    @staticmethod
    def _validate_candidate(
        call: PlannedToolCall, candidates: tuple[ToolCandidate, ...]
    ) -> None:
        candidate = next(
            (
                item
                for item in candidates
                if item.plugin_id == call.plugin_id
                and item.source == call.source
                and item.spec.tool_id == call.request.tool_id
            ),
            None,
        )
        if candidate is None:
            raise ToolNotAvailableError(
                "planned tool is not available for an authorized selected source"
            )
        if (
            call.request.parameters.get("source_id") != call.source.source_id
            or call.request.parameters.get("source_version") != call.source.version
        ):
            raise InvalidToolRequestError(
                "tool request source_id and source_version must match the selected source"
            )
        _validate_json_schema(candidate.spec.input_schema, call.request.parameters)


def _validate_tenant(tenant_id: str) -> None:
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id must be a non-empty string")


def _report_tool_failure(
    planner: object,
    call: PlannedToolCall,
    error: ToolExecutionError | ToolEvidenceTooLargeError,
) -> None:
    report = getattr(planner, "report_tool_failure", None)
    if callable(report):
        report(
            call,
            error.error_type
            if isinstance(error, ToolExecutionError)
            else type(error).__name__,
        )


def _check_deadline(deadline_monotonic: float) -> None:
    if time.monotonic() >= deadline_monotonic:
        raise ReasoningBudgetExceededError("reasoning deadline exceeded")


_SCHEMA_KEYS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "enum",
        "items",
        "minItems",
        "maxItems",
        "description",
        "title",
    }
)
_SCHEMA_KEYS_BY_TYPE = {
    "object": frozenset(
        {"type", "properties", "required", "additionalProperties", "enum", "description", "title"}
    ),
    "array": frozenset(
        {"type", "items", "minItems", "maxItems", "enum", "description", "title"}
    ),
    "string": frozenset(
        {"type", "minLength", "maxLength", "enum", "description", "title"}
    ),
    "integer": frozenset(
        {"type", "minimum", "maximum", "enum", "description", "title"}
    ),
    "number": frozenset(
        {"type", "minimum", "maximum", "enum", "description", "title"}
    ),
    "boolean": frozenset({"type", "enum", "description", "title"}),
}


def _validate_schema_definition(
    schema: Mapping[str, JsonValue], path: str = "$", depth: int = 0
) -> None:
    if depth > 16 or not isinstance(schema, Mapping):
        raise ToolNotAvailableError(f"invalid tool schema at {path}")
    if depth == 0:
        try:
            encoded = json.dumps(
                schema, allow_nan=False, ensure_ascii=True, separators=(",", ":")
            )
        except (TypeError, ValueError) as error:
            raise ToolNotAvailableError("tool schema is not JSON serializable") from error
        if len(encoded.encode("utf-8")) > 32 * 1024:
            raise ToolNotAvailableError("tool schema exceeds 32768 bytes")
    schema_type = schema.get("type")
    if not isinstance(schema_type, str) or schema_type not in _SCHEMA_KEYS_BY_TYPE:
        raise ToolNotAvailableError(f"invalid tool schema type at {path}")
    if set(schema) - _SCHEMA_KEYS_BY_TYPE[schema_type]:
        raise ToolNotAvailableError(f"unsupported tool schema keywords at {path}")
    for key in ("title", "description"):
        if key in schema and not isinstance(schema[key], str):
            raise ToolNotAvailableError(f"invalid {key} in tool schema at {path}")
    enum = schema.get("enum")
    if enum is not None and (
        not isinstance(enum, list) or not 1 <= len(enum) <= 256
    ):
        raise ToolNotAvailableError(f"invalid enum in tool schema at {path}")
    if schema_type == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if (
            not isinstance(properties, Mapping)
            or len(properties) > 128
            or not all(isinstance(key, str) for key in properties)
            or not isinstance(required, list)
            or len(required) > 128
            or not all(isinstance(key, str) for key in required)
            or len(set(required)) != len(required)
        ):
            raise ToolNotAvailableError(f"invalid object schema at {path}")
        additional = schema.get("additionalProperties", True)
        if not isinstance(additional, bool) and not isinstance(additional, Mapping):
            raise ToolNotAvailableError(f"invalid additionalProperties at {path}")
        if additional is False and set(required) - properties.keys():
            raise ToolNotAvailableError(f"required fields are not declared at {path}")
        for key, child_schema in properties.items():
            if not isinstance(child_schema, Mapping):
                raise ToolNotAvailableError(
                    f"invalid property schema at {path}.{key}"
                )
            _validate_schema_definition(child_schema, f"{path}.{key}", depth + 1)
        if isinstance(additional, Mapping):
            _validate_schema_definition(additional, f"{path}.*", depth + 1)
    elif schema_type == "array":
        minimum = _schema_definition_integer(schema, "minItems", 0, path)
        maximum = _schema_definition_integer(schema, "maxItems", None, path)
        if maximum is not None and minimum > maximum:
            raise ToolNotAvailableError(f"invalid item limits at {path}")
        items = schema.get("items")
        if items is not None:
            if not isinstance(items, Mapping):
                raise ToolNotAvailableError(f"invalid array item schema at {path}")
            _validate_schema_definition(items, f"{path}[]", depth + 1)
    elif schema_type == "string":
        minimum = _schema_definition_integer(schema, "minLength", 0, path)
        maximum = _schema_definition_integer(schema, "maxLength", None, path)
        if maximum is not None and minimum > maximum:
            raise ToolNotAvailableError(f"invalid string limits at {path}")
    elif schema_type in {"integer", "number"}:
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if any(
            bound is not None and not _finite_number(bound)
            for bound in (minimum, maximum)
        ):
            raise ToolNotAvailableError(f"invalid numeric limits at {path}")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ToolNotAvailableError(f"invalid numeric limits at {path}")


def _schema_definition_integer(
    schema: Mapping[str, JsonValue], key: str, default: int | None, path: str
) -> int | None:
    value = schema.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ToolNotAvailableError(f"invalid {key} at {path}")
    return value


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        or isinstance(value, float)
        and math.isfinite(value)
    )


def _validate_json_schema(schema: Mapping[str, JsonValue], value: object) -> None:
    _validate_schema_value(schema, value, "$", 0)


def validate_tool_parameters(
    schema: Mapping[str, JsonValue], parameters: Mapping[str, JsonValue]
) -> None:
    """Validate request parameters against a tool schema.

    Raises ``InvalidToolRequestError``. Messages name schema paths and field
    names but never echo parameter values.
    """
    _validate_json_schema(schema, parameters)


def _validate_schema_value(
    schema: Mapping[str, JsonValue], value: object, path: str, depth: int
) -> None:
    if depth > 16 or not isinstance(schema, Mapping):
        raise InvalidToolRequestError(f"invalid tool schema at {path}")
    unknown = set(schema) - _SCHEMA_KEYS
    if unknown:
        raise InvalidToolRequestError(
            f"unsupported tool schema keywords at {path}: {sorted(unknown)}"
        )
    expected = schema.get("type")
    valid = {
        "object": isinstance(value, Mapping),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
    }
    if not isinstance(expected, str) or expected not in valid or not valid[expected]:
        raise InvalidToolRequestError(
            f"{path} must match the declared {expected!r} type"
        )
    enum = schema.get("enum")
    if enum is not None:
        if not isinstance(enum, list):
            raise InvalidToolRequestError(f"invalid enum at {path}")
        if value not in enum:
            raise InvalidToolRequestError(f"{path} is not an allowed value")
    if expected == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if (
            not isinstance(properties, Mapping)
            or not all(isinstance(key, str) for key in properties)
            or not isinstance(required, list)
            or not all(isinstance(key, str) for key in required)
        ):
            raise InvalidToolRequestError(f"invalid object schema at {path}")
        missing = [key for key in required if key not in value]
        if missing:
            raise InvalidToolRequestError(
                f"{path} is missing required fields: {sorted(missing)}"
            )
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidToolRequestError(f"{path} object keys must be strings")
            item_schema = properties.get(key)
            if item_schema is None:
                additional = schema.get("additionalProperties", True)
                if additional is False:
                    raise InvalidToolRequestError(
                        f"{path}.{key} is not an allowed field"
                    )
                if isinstance(additional, Mapping):
                    _validate_schema_value(
                        additional, item, f"{path}.{key}", depth + 1
                    )
                elif not isinstance(additional, bool):
                    raise InvalidToolRequestError(
                        f"invalid additionalProperties at {path}"
                    )
            elif isinstance(item_schema, Mapping):
                _validate_schema_value(item_schema, item, f"{path}.{key}", depth + 1)
            else:
                raise InvalidToolRequestError(
                    f"invalid property schema for {path}.{key}"
                )
    elif expected == "array":
        minimum = _schema_integer(schema, "minItems", 0, path)
        maximum = _schema_integer(schema, "maxItems", None, path)
        if maximum is not None and minimum > maximum:
            raise InvalidToolRequestError(f"invalid item limits at {path}")
        if len(value) < minimum or (
            maximum is not None and len(value) > maximum
        ):
            raise InvalidToolRequestError(f"{path} has an invalid number of items")
        item_schema = schema.get("items")
        if item_schema is not None:
            if not isinstance(item_schema, Mapping):
                raise InvalidToolRequestError(f"invalid array schema at {path}")
            for index, item in enumerate(value):
                _validate_schema_value(
                    item_schema, item, f"{path}[{index}]", depth + 1
                )
    elif expected == "string":
        minimum = _schema_integer(schema, "minLength", 0, path)
        maximum = _schema_integer(schema, "maxLength", None, path)
        if maximum is not None and minimum > maximum:
            raise InvalidToolRequestError(f"invalid string limits at {path}")
        if len(value) < minimum or (
            maximum is not None and len(value) > maximum
        ):
            raise InvalidToolRequestError(f"{path} has an invalid string length")
    elif expected in {"integer", "number"}:
        if isinstance(value, float) and not math.isfinite(value):
            raise InvalidToolRequestError(f"{path} must be finite")
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        for bound in (minimum, maximum):
            if bound is not None and (
                isinstance(bound, bool)
                or not isinstance(bound, (int, float))
                or not math.isfinite(bound)
            ):
                raise InvalidToolRequestError(f"invalid numeric limits at {path}")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise InvalidToolRequestError(f"invalid numeric limits at {path}")
        if minimum is not None and value < minimum:
            raise InvalidToolRequestError(f"{path} is below its minimum")
        if maximum is not None and value > maximum:
            raise InvalidToolRequestError(f"{path} exceeds its maximum")


def _schema_integer(
    schema: Mapping[str, JsonValue], key: str, default: int | None, path: str
) -> int | None:
    value = schema.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidToolRequestError(f"invalid {key} at {path}")
    return value
