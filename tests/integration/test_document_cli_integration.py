from __future__ import annotations

import json
from pathlib import Path

from dataindex.cli import main


def _document_arguments(catalog: Path, source: Path) -> list[str]:
    return [
        "--catalog",
        str(catalog),
        "--tenant",
        "cli-test",
        "--source-id",
        "cli-document",
        "--source-type",
        "text/markdown",
        "--file",
        str(source),
        "--trusted-distribution",
        "dataindex-ext-documents",
        "--trusted-version",
        "0.1.0",
        "--enable-plugin",
    ]


def test_cli_indexes_and_queries_an_explicitly_trusted_document(
    tmp_path: Path, capsys
) -> None:
    catalog = tmp_path / "catalog.sqlite"
    document = tmp_path / "guide.md"
    document.write_text("# Retention\n\nRecords are retained for 7 years.", encoding="utf-8")
    arguments = _document_arguments(catalog, document)

    assert main(["index", *arguments]) == 0
    indexed = json.loads(capsys.readouterr().out)
    assert indexed["indexed_records"] == 1
    assert indexed["source_id"] == "cli-document"

    assert main(["query", *arguments, "--text", "retained records"]) == 0
    evidence = json.loads(capsys.readouterr().out)
    assert evidence["source_id"] == "cli-document"
    assert evidence["source_version"] == indexed["source_version"]
    assert "7 years" in evidence["value"]
    assert evidence["locator"]["scheme"] == "markdown-heading"


def test_cli_requires_explicit_plugin_enablement(tmp_path: Path, capsys) -> None:
    document = tmp_path / "guide.md"
    document.write_text("# Guide\n\nText.", encoding="utf-8")
    arguments = _document_arguments(tmp_path / "catalog.sqlite", document)
    arguments.remove("--enable-plugin")

    assert main(["index", *arguments]) == 2
    assert "--enable-plugin" in capsys.readouterr().err


def test_cli_rejects_a_file_that_changed_after_indexing(
    tmp_path: Path, capsys
) -> None:
    catalog = tmp_path / "catalog.sqlite"
    document = tmp_path / "guide.md"
    document.write_text("# Guide\n\nOriginal content.", encoding="utf-8")
    arguments = _document_arguments(catalog, document)

    assert main(["index", *arguments]) == 0
    capsys.readouterr()
    document.write_text("# Guide\n\nChanged content.", encoding="utf-8")

    assert main(["query", *arguments, "--text", "content"]) == 2
    assert "source is unavailable or file content differs" in capsys.readouterr().err


def test_cli_rejects_an_untrusted_distribution_version(
    tmp_path: Path, capsys
) -> None:
    document = tmp_path / "guide.md"
    document.write_text("# Guide\n\nText.", encoding="utf-8")
    arguments = _document_arguments(tmp_path / "catalog.sqlite", document)
    version_index = arguments.index("--trusted-version") + 1
    arguments[version_index] = "9.9.9"

    assert main(["index", *arguments]) == 2
    assert "not trusted" in capsys.readouterr().err
