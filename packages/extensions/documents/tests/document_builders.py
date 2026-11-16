"""Build small synthetic documents for the format tests.

The hostile-input tests also import this module in a child process, so it must
stay importable without pytest.
"""

from __future__ import annotations

import io
import zipfile
import zlib
from collections.abc import Mapping

PDF_TEXT = b"BT /F1 12 Tf 72 720 Td (hello) Tj ET\n"
_HELVETICA = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
_PAGE = (
    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
    b"/Resources << /Font << /F1 4 0 R%s >>%s >> /Contents %s >>"
)


def pdf_bytes(objects: Mapping[int, bytes], root: int = 1) -> bytes:
    """Serialize numbered PDF objects with a classic cross-reference table."""
    out = io.BytesIO()
    out.write(b"%PDF-1.7\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = out.tell()
        out.write(b"%d 0 obj\n" % number + objects[number] + b"\nendobj\n")
    xref = out.tell()
    size = max(objects) + 1
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % size)
    for number in range(1, size):
        if number in offsets:
            out.write(b"%010d 00000 n \n" % offsets[number])
        else:
            out.write(b"0000000000 65535 f \n")
    out.write(b"trailer\n<< /Size %d /Root %d 0 R >>\n" % (size, root))
    out.write(b"startxref\n%d\n%%%%EOF\n" % xref)
    return out.getvalue()


def pdf_stream(data: bytes, extra: bytes = b"", *, compress: bool = True) -> bytes:
    body = zlib.compress(data, 9) if compress else data
    filters = b" /Filter /FlateDecode" if compress else b""
    return (
        b"<< /Length %d%s%s >>\nstream\n" % (len(body), filters, extra)
        + body
        + b"\nendstream"
    )


def _pdf_document(
    pages: list[tuple[bytes, bytes, bytes]],
    objects: dict[int, bytes],
    outline: int | None = None,
) -> bytes:
    """``pages`` holds (extra fonts, extra resources, contents reference) per page."""
    catalog = b"<< /Type /Catalog /Pages 2 0 R"
    if outline is not None:
        catalog += b" /Outlines %d 0 R" % outline
    objects[1] = catalog + b" >>"
    objects[4] = _HELVETICA
    kids = []
    for index, page in enumerate(pages):
        objects[10 + index] = _PAGE % page
        kids.append(b"%d 0 R" % (10 + index))
    objects[2] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(kids),
        len(kids),
    )
    return pdf_bytes(objects)


def text_pdf(page_texts: list[str]) -> bytes:
    """A PDF with one line of Helvetica text per page."""
    objects: dict[int, bytes] = {}
    pages = []
    for index, text in enumerate(page_texts):
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content = b"BT /F1 12 Tf 72 720 Td (%s) Tj ET\n" % escaped.encode("latin-1")
        objects[1000 + index] = pdf_stream(content)
        pages.append((b"", b"", b"%d 0 R" % (1000 + index)))
    return _pdf_document(pages, objects)


def shared_content_pdf(pages: int, path_ops: int) -> bytes:
    """Many pages that share one large content stream."""
    objects = {3: pdf_stream(PDF_TEXT + b"0 0 m\n" * path_ops)}
    return _pdf_document([(b"", b"", b"3 0 R")] * pages, objects)


def repeated_form_pdf(
    invocations: int, form_ops: int, *, broken: bool = False
) -> bytes:
    """One page that draws the same form XObject ``invocations`` times."""
    if broken:
        form = (
            b"<< /Type /XObject /Subtype /Form /BBox [0 0 10 10] /Length 4 "
            b"/Filter /NoSuchDecode >>\nstream\nabcd\nendstream"
        )
    else:
        form = pdf_stream(
            PDF_TEXT + b"0 0 m\n" * form_ops,
            b" /Type /XObject /Subtype /Form /BBox [0 0 10 10]",
        )
    objects = {5: form, 6: pdf_stream(PDF_TEXT + b"/Fm1 Do\n" * invocations)}
    return _pdf_document([(b"", b" /XObject << /Fm1 5 0 R >>", b"6 0 R")], objects)


def broken_forms_pdf(count: int) -> bytes:
    """One page that draws ``count`` distinct forms whose streams cannot be decoded."""
    objects: dict[int, bytes] = {}
    names = []
    for index in range(count):
        objects[5000 + index] = (
            b"<< /Type /XObject /Subtype /Form /BBox [0 0 10 10] /Length 4 "
            b"/Filter /NoSuchDecode >>\nstream\nabcd\nendstream"
        )
        names.append(b"/Fm%d %d 0 R" % (index, 5000 + index))
    draws = b"".join(b"/Fm%d Do\n" % index for index in range(count))
    objects[6] = pdf_stream(PDF_TEXT + draws)
    resources = b" /XObject << " + b" ".join(names) + b" >>"
    return _pdf_document([(b"", resources, b"6 0 R")], objects)


def font_cmap_pdf(pages: int, entries: int) -> bytes:
    """Pages that share a font whose ToUnicode map has ``entries`` mappings."""
    lines = b"".join(
        b"<%04X> <%04X>\n" % (code, 0x41 + code % 26) for code in range(entries)
    )
    cmap = (
        b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        b"1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
        b"%d beginbfchar\n" % entries + lines + b"endbfchar\nendcmap\nend end\n"
    )
    objects = {
        6: pdf_stream(cmap),
        7: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 6 0 R >>",
        3: pdf_stream(b"BT /F2 12 Tf 72 720 Td (hello) Tj ET\n"),
    }
    page = (b" /F2 7 0 R", b"", b"3 0 R")
    return _pdf_document([page] * pages, objects)


def inflating_streams_pdf(pages: int, decoded_bytes: int) -> bytes:
    """Pages whose distinct content streams each decode to ``decoded_bytes``."""
    objects: dict[int, bytes] = {}
    page_list = []
    for index in range(pages):
        objects[1000 + index] = pdf_stream(PDF_TEXT + b" " * decoded_bytes)
        page_list.append((b"", b"", b"%d 0 R" % (1000 + index)))
    return _pdf_document(page_list, objects)


def page_tree_dag_pdf(levels: int) -> bytes:
    """A page tree whose nodes list the same child eight times: 8**levels leaves."""
    objects = {3: pdf_stream(PDF_TEXT), 10: _PAGE % (b"", b"", b"3 0 R")}
    for level in range(levels):
        child = 100 + level + 1 if level + 1 < levels else 10
        kids = b" ".join([b"%d 0 R" % child] * 8)
        objects[100 + level] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
            kids,
            8 ** (levels - level),
        )
    objects[2] = b"<< /Type /Pages /Kids [100 0 R] /Count %d >>" % (8**levels)
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[4] = _HELVETICA
    return pdf_bytes(objects)


def outline_pdf(entries: int) -> bytes:
    """One page with ``entries`` top-level bookmarks, each with one child."""
    objects: dict[int, bytes] = {3: pdf_stream(PDF_TEXT)}
    first = 1000
    for index in range(entries):
        number = first + 2 * index
        following = b" /Next %d 0 R" % (number + 2) if index + 1 < entries else b""
        objects[number] = (
            b"<< /Title (Part %d) /Parent 5 0 R /Dest [10 0 R /Fit] "
            b"/First %d 0 R /Last %d 0 R /Count 1%s >>"
            % (index, number + 1, number + 1, following)
        )
        objects[number + 1] = (
            b"<< /Title (Detail %d) /Parent %d 0 R /Dest [10 0 R /Fit] >>"
            % (
                index,
                number,
            )
        )
    objects[5] = b"<< /Type /Outlines /First %d 0 R /Last %d 0 R /Count %d >>" % (
        first,
        first + 2 * (entries - 1),
        2 * entries,
    )
    return _pdf_document([(b"", b"", b"3 0 R")], objects, outline=5)


def zip_bytes(
    members: Mapping[str, bytes | str], *, stored: tuple[str, ...] = ()
) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, data in members.items():
            payload = data.encode("utf-8") if isinstance(data, str) else data
            method = zipfile.ZIP_STORED if name in stored else zipfile.ZIP_DEFLATED
            archive.writestr(zipfile.ZipInfo(name), payload, compress_type=method)
    return out.getvalue()


def set_encrypted_flag(archive: bytes) -> bytes:
    """Mark every member as encrypted, as a password-protected zip would."""
    data = bytearray(archive)
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        start = 0
        while (index := data.find(signature, start)) != -1:
            data[index + flag_offset] |= 0x01
            start = index + 4
    return bytes(data)


EPUB_CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""

EPUB_PACKAGE = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="id">urn:uuid:fictional</dc:identifier>
    <dc:title>Fictional handbook</dc:title>
  </metadata>
  <manifest>
    <item id="ch1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="text/ch2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="ch1"/>
    <itemref idref="ch2"/>
  </spine>
</package>"""

EPUB_CHAPTER_ONE = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>One</title></head>
<body>
<h1>Chapter one</h1>
<p>The North warehouse opens at six.</p>
<h2>Deliveries</h2>
<p>Trucks arrive every Tuesday.</p>
<h3>Late trucks</h3>
<p>Late trucks wait at the South gate.</p>
</body></html>"""

EPUB_CHAPTER_TWO = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Two</title></head>
<body><p>Closing notes without a heading.</p></body></html>"""


def epub_bytes(
    chapters: Mapping[str, str] | None = None, encryption: str | None = None
) -> bytes:
    members: dict[str, bytes | str] = {
        "mimetype": "application/epub+zip",
        "META-INF/container.xml": EPUB_CONTAINER,
        "OEBPS/content.opf": EPUB_PACKAGE,
    }
    if encryption is not None:
        members["META-INF/encryption.xml"] = encryption
    for name, body in (
        chapters or {"ch1.xhtml": EPUB_CHAPTER_ONE, "ch2.xhtml": EPUB_CHAPTER_TWO}
    ).items():
        members[f"OEBPS/text/{name}"] = body
    return zip_bytes(members, stored=("mimetype",))


def epub_encryption(algorithm: str) -> str:
    return f"""<?xml version="1.0"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
            xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
  <enc:EncryptedData>
    <enc:EncryptionMethod Algorithm="{algorithm}"/>
    <enc:CipherData><enc:CipherReference URI="OEBPS/text/ch1.xhtml"/></enc:CipherData>
  </enc:EncryptedData>
</encryption>"""


ODF_ENCRYPTED_MANIFEST = """<?xml version="1.0" encoding="UTF-8"?>
<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">
  <manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/>
  <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml">
    <manifest:encryption-data manifest:checksum-type="SHA1" manifest:checksum="AAAA">
      <manifest:algorithm manifest:algorithm-name="Blowfish CFB" manifest:initialisation-vector="AAAA"/>
    </manifest:encryption-data>
  </manifest:file-entry>
</manifest:manifest>"""


def odt_bytes(content_xml: str, manifest: str | None = None) -> bytes:
    members: dict[str, bytes | str] = {
        "mimetype": "application/vnd.oasis.opendocument.text",
        "content.xml": content_xml,
    }
    if manifest is not None:
        members["META-INF/manifest.xml"] = manifest
    return zip_bytes(members, stored=("mimetype",))


def docx_bytes(document_xml: str) -> bytes:
    content_types = (
        '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
        'content-types"><Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxml'
        'formats-officedocument.wordprocessingml.document.main+xml"/></Types>'
    )
    relationships = (
        '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
        'package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
        'openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    )
    return zip_bytes(
        {
            "[Content_Types].xml": content_types,
            "_rels/.rels": relationships,
            "word/document.xml": document_xml,
        }
    )
