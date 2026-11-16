"""Reader for EPUB 2 and 3 e-books (.epub)."""

from __future__ import annotations

import html
import re

from dataindex.models import NativeLocator
from dataindex_ext_documents.exceptions import (
    MalformedDocumentError,
    ProtectedDocumentError,
)
from dataindex_ext_documents.formats._base import (
    MAX_HTML_TAGS,
    MAX_TITLE_CHARS,
    DocumentSection,
    ElementBudget,
    TextBudget,
    check_section_count,
    plain_title,
)
from dataindex_ext_documents.formats._safexml import decode_xml_document, parse_tree
from dataindex_ext_documents.formats._safezip import SafeZip, resolve_part
from dataindex_ext_documents.formats.office import attribute, local_name
from dataindex_ext_documents.formats.text import parse_html_chapter

MAX_SPINE_ITEMS = 5_000
_CONTENT_TYPES = frozenset({"application/xhtml+xml", "text/html"})
# Font obfuscation only scrambles embedded fonts; the text itself is readable.
_FONT_OBFUSCATION = frozenset(
    {"http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"}
)
_TITLE_OPEN = re.compile(r"<title\b[^>]{0,200}>", re.I)
_TITLE_CLOSE = re.compile(r"</title", re.I)


def _package_document(package: SafeZip) -> str:
    container = package.read_optional("META-INF/container.xml")
    if container is None:
        raise MalformedDocumentError("EPUB has no META-INF/container.xml")
    for element in parse_tree(container, label="EPUB container", budget=package.elements).iter():
        if local_name(element.tag) == "rootfile":
            path = resolve_part("", attribute(element.attrib, "full-path") or "")
            if path and package.has(path):
                return path
    raise MalformedDocumentError("EPUB container names no package document")


def _encrypted_parts(package: SafeZip) -> set[str]:
    data = package.read_optional("META-INF/encryption.xml")
    if data is None:
        return set()
    encrypted: set[str] = set()
    for element in parse_tree(data, label="EPUB encryption", budget=package.elements).iter():
        if local_name(element.tag) != "EncryptedData":
            continue
        algorithm = next(
            (
                attribute(child.attrib, "Algorithm") or ""
                for child in element.iter()
                if local_name(child.tag) == "EncryptionMethod"
            ),
            "",
        )
        if algorithm in _FONT_OBFUSCATION:
            continue
        for child in element.iter():
            if local_name(child.tag) == "CipherReference":
                path = resolve_part("", attribute(child.attrib, "URI") or "")
                if path:
                    encrypted.add(path.casefold())
    return encrypted


def _chapter_title(content: str, sections: tuple[DocumentSection, ...], href: str) -> str:
    head = content[:65536]
    opening = _TITLE_OPEN.search(head)
    if opening:
        closing = _TITLE_CLOSE.search(head, opening.end())
        if closing:
            raw = re.sub(r"<[^>]*>", "", head[opening.end() : closing.start()])
            title = plain_title(html.unescape(raw))
            if title:
                return title[:MAX_TITLE_CHARS]
    heading = next((section.title for section in sections if section.kind == "heading"), "")
    return heading or href.rsplit("/", 1)[-1]


def parse_epub(raw: bytes) -> tuple[DocumentSection, ...]:
    package = SafeZip(raw, "EPUB")
    mimetype = package.read_optional("mimetype", limit=256)
    if mimetype is not None and mimetype.strip() != b"application/epub+zip":
        raise MalformedDocumentError("EPUB has a different mimetype")
    opf_path = _package_document(package)
    opf = parse_tree(
        package.read(opf_path), label="EPUB package document", budget=package.elements
    )
    manifest: dict[str, tuple[str, str]] = {}
    spine: list[str] = []
    for element in opf.iter():
        name = local_name(element.tag)
        if name == "item":
            identifier = attribute(element.attrib, "id") or ""
            href = resolve_part(opf_path, attribute(element.attrib, "href") or "")
            if identifier and href:
                manifest[identifier] = (href, attribute(element.attrib, "media-type") or "")
        elif name == "itemref":
            spine.append(attribute(element.attrib, "idref") or "")
    if len(spine) > MAX_SPINE_ITEMS:
        raise MalformedDocumentError(f"EPUB spine has more than {MAX_SPINE_ITEMS} items")
    encrypted = _encrypted_parts(package)
    budget = TextBudget()
    tags = ElementBudget(MAX_HTML_TAGS)
    sections: list[DocumentSection] = []
    for number, idref in enumerate(spine, start=1):
        item = manifest.get(idref)
        if item is None or item[1] not in _CONTENT_TYPES or not package.has(item[0]):
            continue
        href = item[0]
        if href.casefold() in encrypted:
            raise ProtectedDocumentError(
                "EPUB content is DRM-encrypted; only DRM-free e-books can be indexed"
            )
        content = decode_xml_document(package.read(href))
        visible, chapter = parse_html_chapter(content, tags)
        if not budget.add(visible):
            continue
        headings = [section for section in chapter if section.kind == "heading"]
        intro = next(
            (section.text for section in chapter if section.kind == "document_intro"), ""
        )
        sections.append(
            DocumentSection(
                stable_key=f"chapter:{number}",
                title=_chapter_title(content, chapter, href),
                kind="chapter",
                level=1,
                locator=NativeLocator("epub-chapter", 1, {"spine": number, "href": href}),
                search_text=(intro or headings[0].title) if headings else visible,
                text=visible,
            )
        )
        for heading in headings:
            value = heading.locator.value
            sections.append(
                DocumentSection(
                    stable_key=f"chapter:{number}:{heading.stable_key}",
                    title=heading.title,
                    kind="heading",
                    level=heading.level + 1,
                    locator=NativeLocator(
                        "epub-heading",
                        1,
                        {
                            "spine": number,
                            "href": href,
                            "ordinal": value["ordinal"],
                            "anchor": value["anchor"],
                            "line_start": value["line_start"],
                            "line_end": value["line_end"],
                        },
                    ),
                    search_text=heading.search_text,
                    text=heading.text,
                )
            )
        check_section_count(len(sections))
    return tuple(sections)
