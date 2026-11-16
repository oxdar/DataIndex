from __future__ import annotations

import importlib.resources
import json
import sys
from pathlib import Path

import pytest

from dataindex.models import (
    EvidenceKind,
    IndexContext,
    RetrievalContext,
    SourceRef,
    ToolRequest,
)
from dataindex_ext_documents import (
    DocumentIndexExtension,
    DocumentReaderUnavailableError,
    DocumentTooLargeError,
    EmptyDocumentError,
    InvalidDocumentEncodingError,
    MalformedDocumentError,
    ProtectedDocumentError,
    UnsupportedDocumentFormatError,
)
from dataindex_ext_documents.formats import (
    FORMATS,
    FORMATS_BY_SOURCE_TYPE,
    SOURCE_TYPES,
    SUFFIXES,
)
from dataindex_ext_documents.plugin import RETRIEVE_TOOL_ID
from dataindex_ext_documents.readers import document_bytes, format_for, parse_document

from document_builders import (
    EPUB_CHAPTER_ONE,
    ODF_ENCRYPTED_MANIFEST,
    broken_forms_pdf,
    docx_bytes,
    epub_bytes,
    epub_encryption,
    inflating_streams_pdf,
    odt_bytes,
    outline_pdf,
    page_tree_dag_pdf,
    repeated_form_pdf,
    shared_content_pdf,
    text_pdf,
    zip_bytes,
)

FIXTURES = Path(__file__).parent / "fixtures"


def source(source_type: str, source_id: str = "format-test") -> SourceRef:
    return SourceRef(source_id, source_type, "sha256:format-test")


def parse(source_type: str, content: bytes | str):
    return parse_document(source(source_type), content)


def test_format_suffixes_and_size_limits_match_core_and_manifest() -> None:
    from dataindex.local import (
        BINARY_DOCUMENT_SUFFIXES,
        DOCUMENT_SUFFIXES,
        MAX_SUBMITTED_BINARY_FILE_BYTES,
        MAX_SUBMITTED_FILE_BYTES,
    )
    from dataindex_ext_documents.formats._base import (
        MAX_BINARY_DOCUMENT_BYTES,
        MAX_DOCUMENT_BYTES,
    )

    manifest = json.loads(
        importlib.resources.files("dataindex_ext_documents")
        .joinpath("dataindex-extension.json")
        .read_text(encoding="utf-8")
    )
    assert dict(SUFFIXES) == DOCUMENT_SUFFIXES
    assert {
        suffix for spec in FORMATS if spec.binary for suffix in spec.suffixes
    } == BINARY_DOCUMENT_SUFFIXES
    assert manifest["source_types"] == list(SOURCE_TYPES)
    assert MAX_SUBMITTED_FILE_BYTES == MAX_DOCUMENT_BYTES
    assert MAX_SUBMITTED_BINARY_FILE_BYTES == MAX_BINARY_DOCUMENT_BYTES
    assert set(FORMATS_BY_SOURCE_TYPE) == set(SOURCE_TYPES)


@pytest.mark.parametrize(
    ("filename", "expected_titles", "expected_scheme"),
    [
        (
            "supply_agreement.docx",
            [
                "Supply agreement",
                "Payment terms",
                "Late payment",
                "Delivery",
                "Termination",
            ],
            "docx-heading",
        ),
        (
            "supply_agreement.doc",
            [
                "Supply agreement",
                "Payment terms",
                "Late payment",
                "Delivery",
                "Termination",
            ],
            "doc-heading",
        ),
        (
            "supply_agreement.rtf",
            [
                "Supply agreement",
                "Payment terms",
                "Late payment",
                "Delivery",
                "Termination",
            ],
            "rtf-heading",
        ),
        (
            "supply_agreement.odt",
            [
                "Introduction",
                "Payment terms",
                "Late payment",
                "Delivery",
                "Termination",
            ],
            "odt-blocks",
        ),
        (
            "supply_agreement.pdf",
            [
                "Payment terms",
                "Late payment",
                "Page 1: Supply agreement",
                "Delivery",
                "Page 2: Delivery",
                "Termination",
                "Page 3: Termination",
            ],
            "pdf-outline",
        ),
        ("inventory.xlsx", ["Prices", "Stock levels"], "xlsx-sheet"),
        ("inventory.xls", ["Prices", "Stock levels"], "xls-sheet"),
        ("inventory.ods", ["Prices", "Stock_levels"], "ods-sheet"),
        (
            "quarterly_review.pptx",
            [
                "Slide 1: Quarterly review",
                "Slide 2: Revenue",
                "Slide 3: Site headcount",
            ],
            "pptx-slide",
        ),
        (
            "quarterly_review.ppt",
            [
                "Slide 1: Quarterly review",
                "Slide 2: Revenue",
                "Slide 3: Site headcount",
            ],
            "ppt-slide",
        ),
        (
            "quarterly_review.odp",
            [
                "Slide 1: Quarterly review",
                "Slide 2: Revenue",
                "Slide 3: Site headcount",
            ],
            "odp-slide",
        ),
    ],
)
def test_office_and_pdf_fixtures_preserve_sections_and_locators(
    filename: str, expected_titles: list[str], expected_scheme: str
) -> None:
    raw = (FIXTURES / filename).read_bytes()
    spec = format_for(SUFFIXES[Path(filename).suffix])
    missing = spec.missing_modules()
    if missing:
        pytest.skip(f"{spec.label} needs {', '.join(missing)}")

    document = parse_document(source(spec.source_type), raw)

    assert [section.title for section in document.sections] == expected_titles
    assert document.sections[0].locator.scheme == expected_scheme
    assert all(section.locator.value for section in document.sections)
    if filename.endswith(".docx"):
        delivery = next(
            section for section in document.sections if section.title == "Delivery"
        )
        assert delivery.locator.value == {
            "anchor": "delivery",
            "level": 1,
            "block_start": 8,
            "block_end": 10,
        }
        assert "Goods ship from the North warehouse every Tuesday." in delivery.text
        assert document.nodes[2].parent_id == document.nodes[1].node_id
    elif filename == "supply_agreement.pdf":
        assert document.sections[0].locator.value == {
            "ordinal": 1,
            "title": "Payment terms",
            "page_start": 1,
            "page_end": 1,
        }
    elif filename == "inventory.xlsx":
        assert document.sections[0].locator.value == {
            "sheet": "Prices",
            "sheet_index": 1,
            "range": "A1:D6",
        }
        assert "Widget | 3.5 | 2026-03-14 | TRUE" in document.sections[0].text
    elif filename == "quarterly_review.pptx":
        assert document.sections[1].locator.value == {"slide": 2}
        assert "Speaker notes:" in document.sections[1].text


def test_csv_and_tsv_use_row_groups_and_physical_line_locators() -> None:
    content = b'Site,Note\nNorth,"two\nlines"\nSouth,ok\n'
    csv_document = parse("text/csv", content)
    tsv_document = parse("text/tab-separated-values", content.replace(b",", b"\t"))

    assert csv_document.sections[0].locator.scheme == "csv-rows"
    assert csv_document.sections[0].locator.value == {
        "row_start": 2,
        "row_end": 3,
        "line_start": 2,
        "line_end": 4,
    }
    assert "two\nlines" in csv_document.sections[0].text
    assert tsv_document.sections[0].locator.scheme == "tsv-rows"
    assert tsv_document.sections[0].locator.value["row_end"] == 3


def test_large_csv_and_json_arrays_are_grouped_without_losing_ranges() -> None:
    rows = b"Site,Units\n" + b"".join(
        f"S{index},{index}\n".encode() for index in range(250)
    )
    csv_sections = parse("text/csv", rows).sections
    json_sections = parse(
        "application/json",
        b"[" + b",".join(str(index).encode() for index in range(250)) + b"]",
    ).sections

    assert [section.locator.value["row_start"] for section in csv_sections] == [
        2,
        102,
        202,
    ]
    assert [section.locator.value["row_end"] for section in csv_sections] == [
        101,
        201,
        251,
    ]
    assert [section.locator.value for section in json_sections[1:]] == [
        {"pointer": "", "index_start": 0, "index_end": 99},
        {"pointer": "", "index_start": 100, "index_end": 199},
        {"pointer": "", "index_start": 200, "index_end": 249},
    ]


def test_json_jsonl_and_xml_keep_native_pointers_and_lines() -> None:
    document = parse(
        "application/json",
        b'{"sites":[{"name":"North","units":420}],"a/b":{"c~d":1}}',
    )
    escaped = next(section for section in document.sections if section.title == "a/b")
    jsonl = parse("application/x-ndjson", b'{"site":"North"}\n{"site":"South"}\n')
    xml = parse(
        "application/xml",
        b'<?xml version="1.0"?>\n<catalog>\n<site name="North"/>\n<site name="South"/>\n</catalog>',
    )

    assert escaped.locator.value == {"pointer": "/a~1b"}
    assert jsonl.sections[0].locator.value == {"line_start": 1, "line_end": 2}
    assert xml.sections[0].locator.value["path"] == "/catalog"
    assert xml.sections[0].locator.value["line_start"] == 2
    assert xml.sections[0].locator.value["line_end"] == 5


def test_eml_extracts_plain_and_html_body_without_script_text() -> None:
    message = (
        b"From: sender@example.test\r\nSubject: Dispatch\r\n"
        b"Content-Type: multipart/mixed; boundary=BOUNDARY\r\n\r\n"
        b"--BOUNDARY\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
        b"<p>Visible delivery note</p><script>hidden secret</script>\r\n"
        b"--BOUNDARY--\r\n"
    )

    document = parse("message/rfc822", message)

    assert document.sections[0].title == "Dispatch"
    assert document.sections[0].locator.value == {"part": "message"}
    assert "Visible delivery note" in document.sections[0].text
    assert "hidden secret" not in document.sections[0].text


def test_epub_keeps_spine_and_heading_locations() -> None:
    document = parse("application/epub+zip", epub_bytes())

    chapter = next(
        section for section in document.sections if section.kind == "chapter"
    )
    heading = next(
        section for section in document.sections if section.title == "Deliveries"
    )
    assert chapter.locator.value == {
        "spine": 1,
        "href": "OEBPS/text/ch1.xhtml",
    }
    assert heading.locator.value == {
        "spine": 1,
        "href": "OEBPS/text/ch1.xhtml",
        "ordinal": 2,
        "anchor": None,
        "line_start": 6,
        "line_end": 10,
    }
    assert "Trucks arrive every Tuesday." in heading.text


def test_text_and_markup_fixtures_are_extracted_as_native_sections() -> None:
    plain = parse("text/plain", (FIXTURES / "operations.txt").read_bytes())
    markdown = parse("text/markdown", (FIXTURES / "payment_terms.md").read_bytes())
    html = parse("text/html", (FIXTURES / "terms.html").read_bytes())

    assert plain.sections
    assert plain.sections[0].locator.scheme == "text-paragraph"
    assert [section.title for section in markdown.sections] == [
        "Invoice guide",
        "Payment terms",
        "Late payment",
        "Disputes",
    ]
    assert all(
        section.locator.scheme == "markdown-heading" for section in markdown.sections
    )
    assert html.sections
    assert all(section.locator.scheme == "html-heading" for section in html.sections)


def test_protected_documents_and_malformed_inputs_are_typed() -> None:
    protected = (FIXTURES / "protected.docx").read_bytes()

    with pytest.raises(ProtectedDocumentError):
        parse(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            protected,
        )
    with pytest.raises(ProtectedDocumentError):
        parse(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            protected,
        )
    with pytest.raises(ProtectedDocumentError):
        parse(
            "application/epub+zip",
            epub_bytes(
                encryption=epub_encryption(
                    "http://www.w3.org/2001/04/xmlenc#aes256-cbc"
                )
            ),
        )
    with pytest.raises(ProtectedDocumentError):
        parse(
            "application/vnd.oasis.opendocument.text",
            odt_bytes(
                '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"/>',
                ODF_ENCRYPTED_MANIFEST,
            ),
        )
    with pytest.raises(MalformedDocumentError, match="not a Word document"):
        parse(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            (FIXTURES / "inventory.xlsx").read_bytes(),
        )
    with pytest.raises(MalformedDocumentError, match="not a PDF"):
        parse("application/pdf", b"plain text, not a PDF")
    with pytest.raises(MalformedDocumentError, match="inline DTD"):
        parse("application/xml", b'<!DOCTYPE r [<!ENTITY x "x">]><r>&x;</r>')


def test_empty_invalid_encoding_and_unsupported_types_are_typed() -> None:
    with pytest.raises(EmptyDocumentError):
        parse("text/csv", b" \n")
    with pytest.raises(EmptyDocumentError):
        parse("application/json", b"")
    with pytest.raises(EmptyDocumentError):
        parse("text/plain", "")
    with pytest.raises(InvalidDocumentEncodingError):
        parse("text/csv", b"name,\xff\n")
    with pytest.raises(UnsupportedDocumentFormatError):
        parse("application/x-unknown-document", b"anything")
    with pytest.raises(TypeError, match="must be bytes"):
        parse(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "not bytes",
        )


def test_blank_pdf_is_empty() -> None:
    pytest.importorskip("pypdf")
    with pytest.raises(EmptyDocumentError):
        parse("application/pdf", text_pdf([""]))


def test_source_and_archive_encryption_are_rejected() -> None:
    spec = format_for(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    encrypted = zip_bytes(
        {
            "[Content_Types].xml": (
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/word/document.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.'
                'wordprocessingml.document.main+xml"/></Types>'
            ),
            "_rels/.rels": (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                'relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.'
                'org/officeDocument/2006/relationships/officeDocument" '
                'Target="word/document.xml"/></Relationships>'
            ),
            "word/document.xml": "<w:document xmlns:w='urn:w'><w:p>text</w:p></w:document>",
        }
    )
    from document_builders import set_encrypted_flag

    with pytest.raises(ProtectedDocumentError):
        parse(spec.source_type, set_encrypted_flag(encrypted))


def test_size_limits_apply_to_text_and_binary_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import dataindex_ext_documents.formats as formats

    monkeypatch.setattr(formats, "MAX_DOCUMENT_BYTES", 4)
    monkeypatch.setattr(formats, "MAX_BINARY_DOCUMENT_BYTES", 8)
    with pytest.raises(DocumentTooLargeError):
        document_bytes(format_for("text/plain"), b"12345")
    with pytest.raises(DocumentTooLargeError):
        document_bytes(format_for("application/pdf"), b"123456789")


@pytest.mark.parametrize(
    ("filename", "query"),
    [
        ("supply_agreement.docx", "Tuesday"),
        ("inventory.xlsx", "Widget"),
        ("quarterly_review.pptx", "Revenue"),
        ("supply_agreement.pdf", "Delivery"),
    ],
)
def test_plugin_registration_returns_observed_citation_with_native_locator(
    filename: str, query: str
) -> None:
    path = FIXTURES / filename
    spec = format_for(SUFFIXES[path.suffix])
    if spec.missing_modules():
        pytest.skip(f"{spec.label} needs {', '.join(spec.missing_modules())}")
    document = source(spec.source_type, f"fixture-{path.stem}")
    extension = DocumentIndexExtension()
    extension.register_document(document, path.read_bytes())
    nodes = tuple(extension.iter_index_records(document, IndexContext()))

    evidence = extension.run_tool(
        ToolRequest(
            RETRIEVE_TOOL_ID,
            {
                "source_id": document.source_id,
                "source_version": document.version,
                "query": query,
            },
        ),
        RetrievalContext(),
    )

    assert evidence.source_id == document.source_id
    assert evidence.source_version == document.version
    assert evidence.evidence_kind is EvidenceKind.OBSERVED
    assert evidence.operation == RETRIEVE_TOOL_ID
    assert evidence.locator in [node.locator for node in nodes]
    assert query.casefold() in str(evidence.value).casefold()
    extension.close()


@pytest.mark.parametrize(
    ("source_type", "module", "filename"),
    [
        ("application/pdf", "pypdf", "supply_agreement.pdf"),
        ("application/msword", "olefile", "supply_agreement.doc"),
        ("application/vnd.ms-excel", "xlrd", "inventory.xls"),
    ],
)
def test_missing_optional_parser_reports_install_hint(
    monkeypatch: pytest.MonkeyPatch, source_type: str, module: str, filename: str
) -> None:
    from dataindex_ext_documents.plugin import DocumentIndexExtension

    spec = format_for(source_type)
    monkeypatch.setattr(
        "dataindex_ext_documents.formats.importlib.util.find_spec",
        lambda name: None if name == module else object(),
    )
    extension = DocumentIndexExtension()
    result = extension.probe(source(source_type))

    assert not result.supported
    assert module in result.reason
    assert spec.install_hint() in result.reason
    with pytest.raises(DocumentReaderUnavailableError, match=module):
        extension.register_document(
            source(source_type), (FIXTURES / filename).read_bytes()
        )
    extension.close()


def test_pdf_reader_handles_missing_import_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataindex_ext_documents.formats.pdf import parse_pdf

    monkeypatch.setitem(sys.modules, "pypdf", None)
    with pytest.raises(
        DocumentReaderUnavailableError, match="dataindex-ext-documents\\[pdf\\]"
    ):
        parse_pdf(text_pdf(["hello"]))


def test_pdf_work_decode_outline_and_page_tree_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("pypdf")
    from dataindex_ext_documents.formats import pdf

    monkeypatch.setattr(pdf, "MAX_PDF_WORK", 100)
    with pytest.raises(DocumentTooLargeError, match="too costly"):
        parse("application/pdf", shared_content_pdf(1, 200))

    monkeypatch.setattr(pdf, "MAX_PDF_WORK", 1_000_000)
    monkeypatch.setattr(pdf, "MAX_PDF_DECODED_BYTES", 128)
    with pytest.raises(DocumentTooLargeError, match="decode to more than"):
        parse("application/pdf", inflating_streams_pdf(1, 256))

    monkeypatch.setattr(pdf, "MAX_PDF_DECODED_BYTES", 128 * 1024 * 1024)
    monkeypatch.setattr(pdf, "MAX_OUTLINE_ENTRIES", 2)
    page_only = parse("application/pdf", outline_pdf(3))
    assert sum(section.kind == "outline" for section in page_only.sections) == 2
    assert any(section.kind == "page" for section in page_only.sections)

    monkeypatch.setattr(pdf, "MAX_PDF_PAGES", 2)
    with pytest.raises(DocumentTooLargeError):
        parse("application/pdf", page_tree_dag_pdf(5))


def test_pdf_form_work_and_damaged_stream_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("pypdf")
    from dataindex_ext_documents.formats import pdf

    monkeypatch.setattr(pdf, "MAX_PDF_WORK", 200)
    with pytest.raises(DocumentTooLargeError, match="too costly"):
        parse("application/pdf", repeated_form_pdf(4, 4))

    monkeypatch.setattr(pdf, "MAX_PDF_WORK", 32 * 1024 * 1024)
    with pytest.raises(MalformedDocumentError, match="more than 8 streams"):
        parse("application/pdf", broken_forms_pdf(9))


def test_pdf_user_password_is_rejected_but_owner_password_is_accepted() -> None:
    pypdf = pytest.importorskip("pypdf")
    from io import BytesIO

    plain = pypdf.PdfReader(BytesIO(text_pdf(["Confidential terms"])))
    writer = pypdf.PdfWriter(clone_from=plain)
    writer.encrypt(user_password="user", owner_password="owner", algorithm="RC4-128")
    protected = BytesIO()
    writer.write(protected)
    with pytest.raises(ProtectedDocumentError):
        parse("application/pdf", protected.getvalue())

    writer = pypdf.PdfWriter(clone_from=plain)
    writer.encrypt(user_password="", owner_password="owner", algorithm="RC4-128")
    owner_only = BytesIO()
    writer.write(owner_only)
    assert parse("application/pdf", owner_only.getvalue()).sections


def test_epub_tag_budget_and_archive_member_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataindex_ext_documents.formats import _safezip, epub

    monkeypatch.setattr(epub, "MAX_HTML_TAGS", 2)
    with pytest.raises(DocumentTooLargeError):
        parse(
            "application/epub+zip",
            epub_bytes(
                chapters={"ch1.xhtml": EPUB_CHAPTER_ONE, "ch2.xhtml": "<p>text</p>"}
            ),
        )

    monkeypatch.setattr(_safezip, "MAX_ZIP_MEMBERS", 1)
    with pytest.raises(DocumentTooLargeError, match="archive members"):
        parse("application/epub+zip", epub_bytes())


def test_prose_readers_reject_excessive_rows_parts_elements_and_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataindex_ext_documents.formats import data, mail, text

    monkeypatch.setattr(data, "_MAX_TABLE_ROWS", 3)
    with pytest.raises(DocumentTooLargeError, match="section limit"):
        parse("text/csv", b"a\n1\n2\n3\n")

    monkeypatch.setattr(data, "MAX_XML_DOCUMENT_ELEMENTS", 2)
    with pytest.raises(DocumentTooLargeError, match="XML elements"):
        parse("application/xml", b"<r><a/><b/></r>")

    monkeypatch.setattr(mail, "MAX_MIME_PARTS", 2)
    eml = (
        b"Subject: many parts\r\nContent-Type: multipart/mixed; boundary=X\r\n\r\n"
        + b"--X\r\nContent-Type: text/plain\r\n\r\nx\r\n" * 3
        + b"--X--\r\n"
    )
    with pytest.raises(DocumentTooLargeError, match="MIME parts"):
        parse("message/rfc822", eml)

    monkeypatch.setattr(text, "MAX_HTML_TAGS", 2)
    with pytest.raises(DocumentTooLargeError, match="tags"):
        parse("text/html", "<p>one</p><p>two</p>")


def test_zip_total_inflated_bytes_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataindex_ext_documents.formats import _safezip

    monkeypatch.setattr(_safezip, "MAX_TOTAL_INFLATED_BYTES", 128)
    with pytest.raises(DocumentTooLargeError, match="inflates beyond"):
        parse(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            docx_bytes(
                "<w:document xmlns:w='urn:w'><w:p>" + "x" * 200 + "</w:p></w:document>"
            ),
        )
