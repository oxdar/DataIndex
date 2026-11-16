from __future__ import annotations

import shutil
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from dataindex import DataIndexClient

FIXTURES = (
    Path(__file__).parents[2]
    / "packages"
    / "extensions"
    / "documents"
    / "tests"
    / "fixtures"
)


@pytest.mark.parametrize(
    ("filename", "section_title", "locator_scheme"),
    [
        ("supply_agreement.docx", "Delivery", "docx-heading"),
        ("inventory.xlsx", "Prices", "xlsx-sheet"),
        ("quarterly_review.pptx", "Revenue", "pptx-slide"),
        ("supply_agreement.pdf", "Delivery", "pdf-outline"),
        ("operations.csv", "North warehouse", "csv-rows"),
    ],
)
def test_local_client_indexes_and_cites_document_formats(
    tmp_path: Path, filename: str, section_title: str, locator_scheme: str
) -> None:
    if filename.endswith(".pdf"):
        pytest.importorskip("pypdf")
    source = FIXTURES / filename
    if not source.exists():
        if filename == "operations.csv":
            source = tmp_path / filename
            source.write_text(
                "Site,Units\nNorth warehouse,420\nSouth depot,75\n",
                encoding="utf-8",
            )
        else:
            pytest.fail(f"missing integration fixture: {source}")
    submitted_file = tmp_path / filename
    if source != submitted_file:
        shutil.copyfile(source, submitted_file)

    with DataIndexClient(chat=None, storage_path=tmp_path / "store") as client:
        indexed = client.submit_document(submitted_file)
        tree = client.get_tree(str(indexed["doc_id"]))
        sections = list(_walk(tree["result"]))
        node = next(
            section for section in sections if section_title in section["title"]
        )
        evidence = client.get_section(str(indexed["doc_id"]), str(node["node_id"]))

    assert indexed["status"] == "completed"
    assert evidence["source_version"] == indexed["version"]
    assert evidence["operation"] == "documents.get_section"
    assert evidence["locator"]["scheme"] == locator_scheme
    assert section_title.casefold() in str(evidence["text"]).casefold()


def _walk(nodes: object) -> Iterator[Mapping[str, object]]:
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if not isinstance(node, dict):
            continue
        yield node
        yield from _walk(node.get("nodes"))
