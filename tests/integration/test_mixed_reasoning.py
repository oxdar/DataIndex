from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from dataindex import (
    EvidenceRecord,
    GroundedClaim,
    InvalidToolRequestError,
    PlannedToolCall,
    PlannerTurn,
    ReasoningBudget,
    SourceRef,
    ToolRequest,
)
from examples.mixed_sources import (
    DATABASE_SOURCE,
    DOCUMENT_SOURCE,
    DUE_BEFORE,
    DemoPlanner,
    demo_environment,
)

INJECTION_FIXTURE = Path(__file__).parent / "fixtures" / "prompt_injection.md"
QUESTION = (
    "For overdue invoices in the SQLite database, return contract totals "
    "and the relevant Markdown payment terms."
)


def _tool_calls(document_source: SourceRef) -> tuple[PlannedToolCall, ...]:
    return (
        PlannedToolCall(
            "documents",
            document_source,
            ToolRequest(
                "documents.search",
                {
                    "source_id": document_source.source_id,
                    "source_version": document_source.version,
                    "query": "payment terms",
                },
            ),
        ),
        PlannedToolCall(
            "databases",
            DATABASE_SOURCE,
            ToolRequest(
                "databases.query",
                {
                    "source_id": DATABASE_SOURCE.source_id,
                    "source_version": DATABASE_SOURCE.version,
                    "query_id": "invoices.overdue_total_by_contract",
                    "filters": {
                        "due_before": DUE_BEFORE,
                        "is_paid": False,
                    },
                },
            ),
        ),
    )


def _claim_builder(
    evidence: tuple[EvidenceRecord, ...],
) -> tuple[GroundedClaim, ...]:
    document = next(item for item in evidence if item.operation == "documents.search")
    database = next(item for item in evidence if item.operation == "databases.query")
    assert isinstance(document.value, str)
    assert isinstance(database.value, Mapping)
    rows = database.value["rows"]
    assert isinstance(rows, list)
    amounts = "; ".join(
        f"{row['contracts__contract_name']}: {row['overdue_total_cents']} USD cents"
        for row in rows
    )
    return (
        GroundedClaim(
            f"Overdue invoice totals are {amounts}. "
            "Invoices are due net 30 days after receipt.",
            (database.execution_id, document.execution_id),
        ),
    )


def test_mock_planner_answers_mixed_sql_and_document_question_with_citations() -> None:
    with demo_environment() as demo:
        result = demo.engine.run_with_planner(
            tenant_id="tenant-a",
            question=QUESTION,
            sources=demo.sources,
            planner=DemoPlanner(),
            budget=ReasoningBudget(max_tool_calls=4, max_depth=2),
        )

        assert not result.answer.abstained
        assert len(result.tool_calls) == 2
        assert {source.source_id for source in result.selected_sources} == {
            DOCUMENT_SOURCE.source_id,
            DATABASE_SOURCE.source_id,
        }
        claim = result.answer.claims[0]
        assert "Fabrikam Operations: 90000 USD cents" in claim.text
        assert "Northwind Services: 150000 USD cents" in claim.text
        assert "net 30 days after receipt" in claim.text
        assert set(claim.evidence_ids) == {
            evidence.execution_id for evidence in result.answer.evidence
        }
        document = next(
            item for item in result.answer.evidence if item.operation == "documents.search"
        )
        database = next(
            item for item in result.answer.evidence if item.operation == "databases.query"
        )
        assert document.node_id is not None
        assert document.source_version == DOCUMENT_SOURCE.version
        assert document.locator.scheme == "markdown-heading"
        assert database.source_version == DATABASE_SOURCE.version
        assert database.locator.scheme == "sqlite-query"


def test_deterministic_no_model_path_returns_cited_fixture_answer() -> None:
    with demo_environment() as demo:
        result = demo.engine.run_deterministic(
            tenant_id="tenant-a",
            question=QUESTION,
            sources=demo.sources,
            tool_calls=_tool_calls(DOCUMENT_SOURCE),
            claim_builder=_claim_builder,
        )

        assert len(result.tool_calls) == 2
        assert result.answer.claims == _claim_builder(result.answer.evidence)
        assert len(result.answer.evidence) == 2
        assert not result.answer.abstained


def test_prompt_injection_cannot_expand_the_sql_tool_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with demo_environment(document_bytes=INJECTION_FIXTURE.read_bytes()) as demo:
        database_calls = []
        original_run_tool = demo.database_extension.run_tool

        def track_database_call(request, context):
            database_calls.append(request)
            return original_run_tool(request, context)

        monkeypatch.setattr(demo.database_extension, "run_tool", track_database_call)

        class InjectionPlanner:
            def __init__(self) -> None:
                self.turn = 0

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
                self.turn += 1
                if self.turn == 1:
                    document_tool = next(
                        item
                        for item in tools
                        if item.plugin_id == "documents"
                        and item.source.source_id == DOCUMENT_SOURCE.source_id
                    )
                    return PlannerTurn(
                        tool_calls=(
                            PlannedToolCall(
                                "documents",
                                document_tool.source,
                                ToolRequest(
                                    "documents.search",
                                    {
                                        "source_id": document_tool.source.source_id,
                                        "source_version": document_tool.source.version,
                                        "query": "payment terms",
                                    },
                                ),
                            ),
                        ),
                        tokens_used=8,
                    )
                assert any(
                    isinstance(item.value, str)
                    and "SYSTEM INSTRUCTION" in item.value
                    for item in evidence
                )
                database_tool = next(
                    item for item in tools if item.plugin_id == "databases"
                )
                return PlannerTurn(
                    tool_calls=(
                        PlannedToolCall(
                            "databases",
                            database_tool.source,
                            ToolRequest(
                                "databases.query",
                                {
                                    "source_id": database_tool.source.source_id,
                                    "source_version": database_tool.source.version,
                                    "query_id": "invoices.overdue_total_by_contract",
                                    "filters": {},
                                    "sql": "DROP TABLE invoices",
                                },
                            ),
                        ),
                    ),
                    tokens_used=8,
                )

        with pytest.raises(InvalidToolRequestError, match="not an allowed field"):
            demo.engine.run_with_planner(
                tenant_id="tenant-a",
                question=QUESTION,
                sources=demo.sources,
                planner=InjectionPlanner(),
            )

        assert database_calls == []
