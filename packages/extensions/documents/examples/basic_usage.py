from __future__ import annotations

from pathlib import Path

from dataindex.models import IndexContext, RetrievalContext, SourceRef, ToolRequest
from dataindex_ext_documents import DocumentIndexExtension


def main() -> None:
    fixture = Path(__file__).parents[1] / "tests" / "fixtures" / "payment_terms.md"
    source = SourceRef(
        source_id="example-payment-terms",
        source_type="text/markdown",
        version="sha256:example-fixture-v1",
    )
    extension = DocumentIndexExtension()
    extension.register_document(source, fixture.read_bytes())
    for node in extension.iter_index_records(source, IndexContext()):
        print(node.node_id, node.title, dict(node.locator.value))
    evidence = extension.run_tool(
        ToolRequest(
            tool_id="documents.search",
            parameters={
                "source_id": source.source_id,
                "source_version": source.version,
                "query": "net 30",
            },
        ),
        RetrievalContext(),
    )
    print(
        evidence.source_id,
        evidence.source_version,
        dict(evidence.locator.value),
        evidence.operation,
        evidence.value,
    )
    extension.close()


if __name__ == "__main__":
    main()
