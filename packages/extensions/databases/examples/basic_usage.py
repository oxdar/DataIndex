from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

from dataindex.models import IndexContext, RetrievalContext, SourceRef, ToolRequest
from dataindex_ext_databases import (
    AggregateRule,
    ColumnRef,
    FixedFilterRule,
    FilterRule,
    JoinRule,
    QueryDefinition,
    SQLiteDatabaseExtension,
)

FIXTURE = Path(__file__).parent / "fixtures" / "contracts_invoices.sql"
SOURCE = SourceRef(
    "demo-contracts",
    "application/vnd.sqlite3",
    "sqlite-live:demo-v1",
)


def query_definitions() -> tuple[QueryDefinition, ...]:
    invoices = "invoices"
    contracts = "contracts"
    due_date = ColumnRef(invoices, "due_date")
    paid = ColumnRef(invoices, "paid")
    invoice_tenant = ColumnRef(invoices, "tenant_id")
    contract_tenant = ColumnRef(contracts, "tenant_id")
    contract_id = ColumnRef(contracts, "contract_id")
    contract_name = ColumnRef(contracts, "contract_name")
    return (
        QueryDefinition(
            query_id="invoices.overdue",
            table=invoices,
            columns=(
                ColumnRef(invoices, "invoice_id"),
                ColumnRef(invoices, "contract_id"),
                ColumnRef(invoices, "amount_cents"),
                due_date,
            ),
            filters=(
                FilterRule("due_before", due_date, "lte"),
                FilterRule("is_paid", paid),
            ),
            source_filters=(
                FixedFilterRule("invoice_tenant", invoice_tenant, "eq", "tenant-a"),
            ),
            order_by=(due_date, ColumnRef(invoices, "invoice_id")),
            max_rows=25,
            unit="USD cents",
        ),
        QueryDefinition(
            query_id="invoices.overdue_total_by_contract",
            table=invoices,
            columns=(contract_id, contract_name),
            filters=(
                FilterRule("due_before", due_date, "lte"),
                FilterRule("is_paid", paid),
            ),
            source_filters=(
                FixedFilterRule("invoice_tenant", invoice_tenant, "eq", "tenant-a"),
                FixedFilterRule("contract_tenant", contract_tenant, "eq", "tenant-a"),
            ),
            joins=(
                JoinRule(
                    ColumnRef(invoices, "contract_id"),
                    contract_id,
                ),
            ),
            aggregate=AggregateRule(
                "sum",
                ColumnRef(invoices, "amount_cents"),
                "overdue_total_cents",
            ),
            group_by=(contract_id, contract_name),
            max_rows=25,
            unit="USD cents",
        ),
        QueryDefinition(
            query_id="invoices.overdue_count",
            table=invoices,
            columns=(),
            filters=(
                FilterRule("due_before", due_date, "lte"),
                FilterRule("is_paid", paid),
            ),
            source_filters=(
                FixedFilterRule("invoice_tenant", invoice_tenant, "eq", "tenant-a"),
            ),
            aggregate=AggregateRule("count", None, "invoice_count"),
            max_rows=1,
        ),
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        database_path = Path(temporary_directory) / "contracts.sqlite"
        connection = sqlite3.connect(database_path)
        try:
            connection.executescript(FIXTURE.read_text(encoding="utf-8"))
        finally:
            connection.close()
        extension = SQLiteDatabaseExtension()
        extension.register_database(
            SOURCE,
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
            queries=query_definitions(),
        )
        for node in extension.iter_index_records(SOURCE, IndexContext()):
            print(node.kind, node.title, dict(node.locator.value))
        evidence = extension.run_tool(
            ToolRequest(
                "databases.query",
                {
                    "source_id": SOURCE.source_id,
                    "source_version": SOURCE.version,
                    "query_id": "invoices.overdue_total_by_contract",
                    "filters": {
                        "due_before": "2026-02-28",
                        "is_paid": False,
                    },
                },
            ),
            RetrievalContext(),
        )
        print(
            evidence.source_id,
            evidence.source_version,
            dict(evidence.locator.value),
            evidence.operation,
            json.dumps(evidence.value, indent=2),
        )
        extension.close()


if __name__ == "__main__":
    main()
