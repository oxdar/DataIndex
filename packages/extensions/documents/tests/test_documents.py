from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

import pytest

from dataindex.catalog import SQLiteCatalog
from dataindex.models import (
    DataIndexExtension,
    EvidenceKind,
    ExtensionManifest,
    IndexContext,
    RetrievalContext,
    SourceRef,
    ToolRequest,
)
from dataindex.registry import ExtensionRegistry, ENTRY_POINT_GROUP
from dataindex_ext_documents import (
    DocumentIndexExtension,
    DocumentDeadlineExceededError,
    DocumentNotRegisteredError,
    DocumentSectionNotFoundError,
    DocumentSourceConflictError,
    DocumentTooLargeError,
    EmptyDocumentError,
    InvalidDocumentEncodingError,
    InvalidDocumentToolRequestError,
    NoDocumentMatchError,
    UnsupportedDocumentFormatError,
)
from dataindex_ext_documents.plugin import (
    DISTRIBUTION,
    PLUGIN_VERSION,
    RETRIEVE_TOOL_ID,
    SECTION_TOOL_ID,
)

FIXTURES = Path(__file__).parent / "fixtures"


def source(
    source_id: str = "fixture",
    source_type: str = "text/markdown",
    version: str = "sha256:fixture-v1",
) -> SourceRef:
    return SourceRef(source_id, source_type, version)


def test_markdown_preserves_heading_hierarchy_and_native_lines() -> None:
    document = source()
    extension = DocumentIndexExtension()
    extension.register_document(
        document, (FIXTURES / "payment_terms.md").read_bytes()
    )

    nodes = tuple(extension.iter_index_records(document, IndexContext()))
    by_title = {node.title: node for node in nodes}

    assert set(by_title) == {
        "Invoice guide",
        "Payment terms",
        "Late payment",
        "Disputes",
    }
    assert by_title["Invoice guide"].parent_id is None
    assert by_title["Payment terms"].parent_id == by_title["Invoice guide"].node_id
    assert by_title["Late payment"].parent_id == by_title["Payment terms"].node_id
    assert by_title["Disputes"].parent_id == by_title["Invoice guide"].node_id
    assert by_title["Payment terms"].locator.scheme == "markdown-heading"
    assert by_title["Payment terms"].locator.value == {
        "anchor": "payment-terms",
        "level": 2,
        "line_start": 5,
        "line_end": 12,
    }
    extension.close()


def test_plain_text_indexes_natural_paragraphs_not_fixed_chunks() -> None:
    document = source("operations", "text/plain", "rev-2")
    extension = DocumentIndexExtension()
    extension.register_document(document, (FIXTURES / "operations.txt").read_bytes())

    nodes = tuple(extension.iter_index_records(document, IndexContext()))

    assert [node.title for node in nodes] == [
        "North site status",
        "South site status",
    ]
    assert [node.locator.value for node in nodes] == [
        {"paragraph": 1, "line_start": 1, "line_end": 2},
        {"paragraph": 2, "line_start": 4, "line_end": 5},
    ]
    assert nodes[0].locator.scheme == "text-paragraph"
    extension.close()


def test_html_extracts_visible_sections_and_ignores_active_content() -> None:
    document = source("agreement", "text/html", "html-rev-1")
    extension = DocumentIndexExtension()
    extension.register_document(document, (FIXTURES / "terms.html").read_bytes())
    nodes = tuple(extension.iter_index_records(document, IndexContext()))
    payment_node = next(node for node in nodes if node.title == "Payment terms")

    evidence = extension.run_tool(
        ToolRequest(
            RETRIEVE_TOOL_ID,
            {
                "source_id": document.source_id,
                "source_version": document.version,
                "query": "fictional invoices due",
            },
        ),
        RetrievalContext(),
    )

    assert payment_node.locator.scheme == "html-heading"
    assert payment_node.locator.value["anchor"] == "payment-terms"
    assert evidence.source_id == document.source_id
    assert evidence.source_version == document.version
    assert evidence.locator == payment_node.locator
    assert "Fictional invoices are due in 30 days." in evidence.value
    assert "this must not be indexed" not in evidence.value
    with pytest.raises(NoDocumentMatchError):
        extension.run_tool(
            ToolRequest(
                RETRIEVE_TOOL_ID,
                {
                    "source_id": document.source_id,
                    "source_version": document.version,
                    "query": "must not be indexed",
                },
            ),
            RetrievalContext(),
        )
    extension.close()


def test_retrieval_returns_observed_citation_that_catalog_verifies() -> None:
    document = source()
    extension = DocumentIndexExtension()
    extension.register_document(
        document, (FIXTURES / "payment_terms.md").read_bytes()
    )
    catalog = SQLiteCatalog()
    catalog.create_source(document)
    nodes = tuple(extension.iter_index_records(document, IndexContext()))
    for node in nodes:
        catalog.create_node(node)

    evidence = extension.run_tool(
        ToolRequest(
            RETRIEVE_TOOL_ID,
            {
                "source_id": document.source_id,
                "source_version": document.version,
                "query": "net 30 days",
            },
        ),
        RetrievalContext(),
    )
    evidence_id = catalog.create_evidence(evidence, extension)
    stored = catalog.get_evidence(evidence_id)

    assert stored == evidence
    assert stored.evidence_kind is EvidenceKind.OBSERVED
    assert stored.node_id is not None
    assert stored.operation == RETRIEVE_TOOL_ID
    catalog.close()
    extension.close()


def test_source_tool_discovery_requires_a_registered_revision() -> None:
    document = source()
    extension = DocumentIndexExtension()

    assert extension.tools_for_source(document) == ()

    extension.register_document(document, (FIXTURES / "payment_terms.md").read_bytes())
    tools = extension.tools_for_source(document)

    assert [tool.tool_id for tool in tools] == [RETRIEVE_TOOL_ID, SECTION_TOOL_ID]
    extension.close()


def test_get_section_returns_exact_observed_section_that_catalog_verifies() -> None:
    document = source()
    extension = DocumentIndexExtension()
    extension.register_document(
        document, (FIXTURES / "payment_terms.md").read_bytes()
    )
    catalog = SQLiteCatalog()
    catalog.create_source(document)
    nodes = tuple(extension.iter_index_records(document, IndexContext()))
    for node in nodes:
        catalog.create_node(node)
    payment = next(node for node in nodes if node.title == "Payment terms")

    evidence = extension.run_tool(
        ToolRequest(
            SECTION_TOOL_ID,
            {
                "source_id": document.source_id,
                "source_version": document.version,
                "node_id": payment.node_id,
            },
        ),
        RetrievalContext(),
    )
    stored = catalog.get_evidence(catalog.create_evidence(evidence, extension))

    assert stored == evidence
    assert evidence.operation == SECTION_TOOL_ID
    assert evidence.evidence_kind is EvidenceKind.OBSERVED
    assert evidence.node_id == payment.node_id
    assert evidence.locator == payment.locator
    assert evidence.value == extension.dereference(document, payment.locator)
    assert evidence.value.startswith("## Payment terms")
    assert "### Late payment" in evidence.value
    catalog.close()
    extension.close()


def test_get_section_rejects_unknown_nodes_bad_requests_and_deadlines() -> None:
    document = source()
    extension = DocumentIndexExtension()
    extension.register_document(
        document, (FIXTURES / "payment_terms.md").read_bytes()
    )
    node = next(iter(extension.iter_index_records(document, IndexContext())))
    base = {"source_id": document.source_id, "source_version": document.version}

    with pytest.raises(DocumentSectionNotFoundError):
        extension.run_tool(
            ToolRequest(SECTION_TOOL_ID, {**base, "node_id": "doc-missing"}),
            RetrievalContext(),
        )
    with pytest.raises(InvalidDocumentToolRequestError, match="node_id"):
        extension.run_tool(ToolRequest(SECTION_TOOL_ID, base), RetrievalContext())
    with pytest.raises(InvalidDocumentToolRequestError, match="node_id"):
        extension.run_tool(
            ToolRequest(
                SECTION_TOOL_ID,
                {**base, "node_id": node.node_id, "query": "payment"},
            ),
            RetrievalContext(),
        )
    with pytest.raises(InvalidDocumentToolRequestError, match="at most"):
        extension.run_tool(
            ToolRequest(SECTION_TOOL_ID, {**base, "node_id": "x" * 129}),
            RetrievalContext(),
        )
    with pytest.raises(DocumentNotRegisteredError):
        extension.run_tool(
            ToolRequest(
                SECTION_TOOL_ID,
                {
                    "source_id": document.source_id,
                    "source_version": "another-version",
                    "node_id": node.node_id,
                },
            ),
            RetrievalContext(),
        )
    with pytest.raises(DocumentDeadlineExceededError):
        extension.run_tool(
            ToolRequest(SECTION_TOOL_ID, {**base, "node_id": node.node_id}),
            RetrievalContext(deadline_monotonic=0),
        )
    extension.close()


def test_probe_registration_and_clear_unsupported_format_errors() -> None:
    extension = DocumentIndexExtension()
    unsupported = source("unknown", "image/png", "rev-png")

    result = extension.probe(unsupported)

    assert not result.supported
    assert "unsupported" in result.reason
    with pytest.raises(UnsupportedDocumentFormatError, match="unsupported"):
        extension.register_document(unsupported, b"\x89PNG\r\n\x1a\n")
    with pytest.raises(DocumentNotRegisteredError):
        tuple(extension.iter_index_records(source(), IndexContext()))
    extension.close()


def test_registration_rejects_bad_encoding_empty_oversized_and_conflicting_data() -> None:
    extension = DocumentIndexExtension()
    document = source()

    with pytest.raises(InvalidDocumentEncodingError):
        extension.register_document(document, b"\xff")
    with pytest.raises(EmptyDocumentError):
        extension.register_document(document, b" \n\t")
    with pytest.raises(DocumentTooLargeError):
        extension.register_document(document, b"x" * (5 * 1024 * 1024 + 1))

    extension.register_document(document, b"# Original\n")
    with pytest.raises(DocumentSourceConflictError):
        extension.register_document(document, b"# Different content\n")
    extension.close()


def test_new_source_revision_replaces_stale_memory_snapshot() -> None:
    extension = DocumentIndexExtension()
    old_source = source(version="rev-1")
    new_source = source(version="rev-2")
    extension.register_document(old_source, b"# Terms\nNet 30 applies.\n")
    old_nodes = tuple(extension.iter_index_records(old_source, IndexContext()))

    extension.register_document(new_source, b"# Terms\nNet 45 applies.\n")

    new_nodes = tuple(extension.iter_index_records(new_source, IndexContext()))
    assert [node.node_id for node in new_nodes] == [node.node_id for node in old_nodes]
    with pytest.raises(DocumentNotRegisteredError):
        tuple(extension.iter_index_records(old_source, IndexContext()))
    extension.close()


def test_unregister_document_drops_every_revision_of_one_source() -> None:
    extension = DocumentIndexExtension()
    first = source(version="rev-1")
    other = source(source_id="other", version="rev-1")
    extension.register_document(first, b"# Terms\nNet 30 applies.\n")
    extension.register_document(other, b"# Other\nKept.\n")

    assert extension.unregister_document("fixture") == 1
    assert extension.unregister_document("fixture") == 0

    assert extension.tools_for_source(first) == ()
    with pytest.raises(DocumentNotRegisteredError):
        tuple(extension.iter_index_records(first, IndexContext()))
    assert len(tuple(extension.iter_index_records(other, IndexContext()))) > 0
    extension.close()


def test_tool_request_is_bounded_and_deadlines_fail_explicitly() -> None:
    extension = DocumentIndexExtension()
    document = source()
    extension.register_document(document, b"# Terms\nNet 30 applies.\n")

    with pytest.raises(InvalidDocumentToolRequestError, match="unknown"):
        extension.run_tool(
            ToolRequest("documents.execute", {}),
            RetrievalContext(),
        )
    with pytest.raises(InvalidDocumentToolRequestError, match="query"):
        extension.run_tool(
            ToolRequest(
                RETRIEVE_TOOL_ID,
                {"source_id": document.source_id, "source_version": document.version},
            ),
            RetrievalContext(),
        )
    with pytest.raises(InvalidDocumentToolRequestError, match="at most"):
        extension.run_tool(
            ToolRequest(
                RETRIEVE_TOOL_ID,
                {
                    "source_id": document.source_id,
                    "source_version": document.version,
                    "query": "x" * 257,
                },
            ),
            RetrievalContext(),
        )
    with pytest.raises(InvalidDocumentToolRequestError, match="searchable word"):
        extension.run_tool(
            ToolRequest(
                RETRIEVE_TOOL_ID,
                {
                    "source_id": document.source_id,
                    "source_version": document.version,
                    "query": "!!!",
                },
            ),
            RetrievalContext(),
        )
    with pytest.raises(DocumentDeadlineExceededError):
        extension.run_tool(
            ToolRequest(
                RETRIEVE_TOOL_ID,
                {
                    "source_id": document.source_id,
                    "source_version": document.version,
                    "query": "net",
                },
            ),
            RetrievalContext(deadline_monotonic=0),
        )
    extension.close()


def _documents_entry_point():
    return next(
        point
        for point in entry_points(group=ENTRY_POINT_GROUP)
        if point.name == "documents"
    )


def test_entry_point_metadata_discovery_and_explicit_enablement() -> None:
    point = _documents_entry_point()
    distribution_name = point.dist.name
    distribution_version = point.dist.version
    assert distribution_name == DISTRIBUTION
    assert distribution_version == PLUGIN_VERSION

    disabled = ExtensionRegistry(
        trusted_distributions={(distribution_name, distribution_version)}
    )
    status = disabled.discover((point,))[0]
    assert status.compatible and status.trusted and not status.enabled
    assert disabled.load_enabled()[0].healthy is None
    assert "documents" not in disabled.loaded_extensions

    enabled = ExtensionRegistry(
        trusted_distributions={(distribution_name, distribution_version)},
        enabled_plugin_ids={"documents"},
    )
    discovered = enabled.discover((point,))[0]
    assert discovered.compatible and discovered.trusted and discovered.enabled
    loaded = enabled.load_enabled()[0]
    assert loaded.healthy
    assert isinstance(enabled.loaded_extensions["documents"], DataIndexExtension)
    assert isinstance(
        enabled.loaded_extensions["documents"].manifest(), ExtensionManifest
    )
    enabled.loaded_extensions["documents"].close()

