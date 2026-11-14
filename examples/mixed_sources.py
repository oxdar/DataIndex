from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, cast

from dataindex import (
    EvidenceRecord,
    ExtensionRegistry,
    GroundedClaim,
    IndexContext,
    PlannedToolCall,
    PlannerTurn,
    ReasoningBudget,
    ReasoningEngine,
    SQLiteCatalog,
    ToolCandidate,
    ToolDispatcher,
    SourceRef,
    ToolRequest,
)
from dataindex_ext_databases import (
    AggregateRule,
    ColumnRef,
    FixedFilterRule,
    FilterRule,
    JoinRule,
    QueryDefinition,
    SQLiteDatabaseExtension,
)
from dataindex_ext_documents import DocumentIndexExtension

ROOT = Path(__file__).resolve().parents[1]
DOCUMENT_FIXTURE = (
    ROOT
    / "packages"
    / "extensions"
    / "documents"
    / "tests"
    / "fixtures"
    / "payment_terms.md"
)
DATABASE_FIXTURE = (
    ROOT
    / "packages"
    / "extensions"
    / "databases"
    / "examples"
    / "fixtures"
    / "contracts_invoices.sql"
)
DUE_BEFORE = "2026-02-28"
DOCUMENT_SOURCE = SourceRef(
    "demo-payment-terms", "text/markdown", "sha256:payment-terms-v1"
)
DATABASE_SOURCE = SourceRef(
    "demo-contracts", "application/vnd.sqlite3", "sqlite-live:demo-v1"
)


def _query_definitions() -> tuple[QueryDefinition, ...]:
    invoices = "invoices"
    contracts = "contracts"
    due_date = ColumnRef(invoices, "due_date")
    paid = ColumnRef(invoices, "paid")
    contract_id = ColumnRef(contracts, "contract_id")
    contract_name = ColumnRef(contracts, "contract_name")
    return (
        QueryDefinition(
            query_id="invoices.overdue_total_by_contract",
            table=invoices,
            columns=(contract_id, contract_name),
            filters=(
                FilterRule("due_before", due_date, "lte"),
                FilterRule("is_paid", paid),
            ),
            source_filters=(
                FixedFilterRule(
                    "invoice_tenant", ColumnRef(invoices, "tenant_id"), "eq", "tenant-a"
                ),
                FixedFilterRule(
                    "contract_tenant",
                    ColumnRef(contracts, "tenant_id"),
                    "eq",
                    "tenant-a",
                ),
            ),
            joins=(JoinRule(ColumnRef(invoices, "contract_id"), contract_id),),
            aggregate=AggregateRule(
                "sum",
                ColumnRef(invoices, "amount_cents"),
                "overdue_total_cents",
            ),
            group_by=(contract_id, contract_name),
            max_rows=25,
            unit="USD cents",
        ),
    )


@dataclass(slots=True)
class DemoEnvironment:
    engine: ReasoningEngine
    sources: tuple[SourceRef, ...]
    catalog: SQLiteCatalog
    database_extension: SQLiteDatabaseExtension


@contextmanager
def demo_environment(
    *, document_bytes: bytes | None = None
) -> Generator[DemoEnvironment, None, None]:
    document_source = (
        DOCUMENT_SOURCE
        if document_bytes is None
        else SourceRef(
            DOCUMENT_SOURCE.source_id,
            DOCUMENT_SOURCE.source_type,
            f"sha256:{hashlib.sha256(document_bytes).hexdigest()}",
        )
    )
    registry = ExtensionRegistry(
        trusted_distributions={
            ("dataindex-ext-documents", "0.1.0"),
            ("dataindex-ext-databases", "0.1.0"),
        },
        enabled_plugin_ids={"documents", "databases"},
    )
    registry.discover()
    registry.load_enabled()
    statuses = {status.plugin_id: status for status in registry.statuses}
    for plugin_id in ("documents", "databases"):
        status = statuses.get(plugin_id)
        if status is None or status.healthy is not True:
            raise RuntimeError(
                f"required extension {plugin_id!r} did not load: "
                f"{None if status is None else status.error}"
            )
    document_extension = cast(
        DocumentIndexExtension, registry.loaded_extensions["documents"]
    )
    database_extension = cast(
        SQLiteDatabaseExtension, registry.loaded_extensions["databases"]
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        database_path = Path(temporary_directory) / "contracts.sqlite"
        connection = sqlite3.connect(database_path)
        try:
            connection.executescript(DATABASE_FIXTURE.read_text(encoding="utf-8"))
        finally:
            connection.close()

        document_extension.register_document(
            document_source,
            document_bytes if document_bytes is not None else DOCUMENT_FIXTURE.read_bytes(),
        )
        database_extension.register_database(
            DATABASE_SOURCE,
            database_path,
            allowed_columns={
                "contracts": (
                    "contract_id",
                    "contract_name",
                    "currency",
                    "tenant_id",
                ),
                "invoices": (
                    "invoice_id",
                    "contract_id",
                    "amount_cents",
                    "due_date",
                    "paid",
                    "tenant_id",
                    "reference",
                ),
            },
            queries=_query_definitions(),
        )

        catalog = SQLiteCatalog()
        catalog.create_source(document_source, tenant_id="tenant-a")
        catalog.create_source(DATABASE_SOURCE, tenant_id="tenant-a")
        for extension, source in (
            (document_extension, document_source),
            (database_extension, DATABASE_SOURCE),
        ):
            for node in extension.iter_index_records(source, IndexContext()):
                catalog.create_node(node)
        dispatcher = ToolDispatcher(
            registry,
            catalog,
            _DemoAuthorizer(),
            deployment="demo-local",
        )
        environment = DemoEnvironment(
            engine=ReasoningEngine(dispatcher, catalog),
            sources=(document_source, DATABASE_SOURCE),
            catalog=catalog,
            database_extension=database_extension,
        )
        try:
            yield environment
        finally:
            catalog.close()
            for extension in registry.loaded_extensions.values():
                extension.close()


class _DemoAuthorizer:
    def is_authorized(
        self, tenant_id: str, source: SourceRef, capability: str
    ) -> bool:
        return tenant_id == "tenant-a" and source.source_id in {
            DOCUMENT_SOURCE.source_id,
            DATABASE_SOURCE.source_id,
        } and capability in {
            "source.access",
            "document.retrieve",
            "database.query",
        }


class DemoPlanner:
    """Synthetic planner for the example; production providers are optional."""

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
        if time.monotonic() >= deadline_monotonic:
            raise TimeoutError("demo planner deadline exceeded")
        if (
            "overdue" not in question.casefold()
            or "payment terms" not in question.casefold()
            or depth > 1
            or remaining_tool_calls < 2
            or remaining_tokens < 16
        ):
            return PlannerTurn()
        if not evidence:
            document_tool = self._find_tool(tools, "documents", "documents.search")
            database_tool = self._find_tool(tools, "databases", "databases.query")
            return PlannerTurn(
                tool_calls=(
                    PlannedToolCall(
                        document_tool.plugin_id,
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
                    PlannedToolCall(
                        database_tool.plugin_id,
                        database_tool.source,
                        ToolRequest(
                            "databases.query",
                            {
                                "source_id": database_tool.source.source_id,
                                "source_version": database_tool.source.version,
                                "query_id": "invoices.overdue_total_by_contract",
                                "filters": {
                                    "due_before": DUE_BEFORE,
                                    "is_paid": False,
                                },
                            },
                        ),
                    ),
                ),
                tokens_used=16,
            )

        document_evidence = next(
            item for item in evidence if item.operation == "documents.search"
        )
        database_evidence = next(
            item for item in evidence if item.operation == "databases.query"
        )
        if not isinstance(document_evidence.value, str):
            raise ValueError("document fixture did not return text evidence")
        if not isinstance(database_evidence.value, Mapping):
            raise ValueError("database fixture did not return structured evidence")
        rows = database_evidence.value.get("rows")
        if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
            raise ValueError("database fixture did not return aggregate rows")
        totals = sorted(
            (
                str(row["contracts__contract_name"]),
                int(row["overdue_total_cents"]),
            )
            for row in rows
        )
        if totals != [
            ("Fabrikam Operations", 90_000),
            ("Northwind Services", 150_000),
        ]:
            raise ValueError(f"unexpected invoice totals: {totals!r}")
        if "net 30 days after receipt" not in document_evidence.value:
            raise ValueError("document evidence did not contain the payment term")
        amount_text = "; ".join(
            f"{name}: {amount} USD cents" for name, amount in totals
        )
        claim = GroundedClaim(
            f"Overdue invoice totals are {amount_text}. "
            "Invoices are due net 30 days after receipt.",
            (database_evidence.execution_id, document_evidence.execution_id),
        )
        return PlannerTurn(claims=(claim,), tokens_used=16)

    @staticmethod
    def _find_tool(
        tools: tuple[ToolCandidate, ...],
        plugin_id: str,
        tool_id: str,
    ) -> ToolCandidate:
        for tool in tools:
            if tool.plugin_id == plugin_id and tool.spec.tool_id == tool_id:
                return tool
        raise ValueError(f"required tool {plugin_id}.{tool_id} was not planned")


def main() -> None:
    question = (
        "For overdue invoices in the SQLite database, return contract totals "
        "and the relevant Markdown payment terms."
    )
    with demo_environment() as demo:
        result = demo.engine.run_with_planner(
            tenant_id="tenant-a",
            question=question,
            sources=demo.sources,
            planner=DemoPlanner(),
            budget=ReasoningBudget(max_tool_calls=4, max_depth=2),
        )
        for claim in result.answer.claims:
            print(claim.text)
        for item in result.answer.evidence:
            print(
                json.dumps(
                    {
                        "evidence_id": item.execution_id,
                        "source_id": item.source_id,
                        "source_version": item.source_version,
                        "locator": dict(item.locator.value),
                        "operation": item.operation,
                    },
                    sort_keys=True,
                )
            )


if __name__ == "__main__":
    main()
