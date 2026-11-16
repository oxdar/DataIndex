"""Reader for e-mail messages (.eml, RFC 5322 / MIME)."""

from __future__ import annotations

import re
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

from dataindex.models import NativeLocator
from dataindex_ext_documents.exceptions import (
    DocumentTooLargeError,
    EmptyDocumentError,
    MalformedDocumentError,
)
from dataindex_ext_documents.formats._base import (
    MAX_TITLE_CHARS,
    DocumentSection,
    TextBudget,
    check_section_count,
    plain_title,
    short_title,
)
from dataindex_ext_documents.formats.text import html_visible_text

_HEADERS = ("Subject", "From", "To", "Cc", "Date")
MAX_ATTACHMENT_NAMES = 200
MAX_MIME_PARTS = 1_000
MAX_MIME_BOUNDARIES = 100
_BOUNDARY = re.compile(rb'boundary\s*=\s*(?:"([^"\r\n]{1,200})"|([^\s;"]{1,200}))', re.I)


def _check_part_count(raw: bytes) -> None:
    """Refuse messages with very many MIME parts before the parser sees them.

    The standard library parser spends about half a millisecond on each part, so
    a large message made of tiny parts would otherwise take minutes. Each part
    starts with a delimiter line, ``--`` and a boundary declared in a header.
    """
    boundaries: set[bytes] = set()
    for match in _BOUNDARY.finditer(raw):
        boundaries.add(match.group(1) or match.group(2))
        if len(boundaries) > MAX_MIME_BOUNDARIES:
            raise DocumentTooLargeError(
                f"message has more than {MAX_MIME_BOUNDARIES} multipart sections"
            )
    parts = sum(raw.count(b"\n--" + boundary) for boundary in boundaries)
    if parts > MAX_MIME_PARTS:
        raise DocumentTooLargeError(f"message has more than {MAX_MIME_PARTS} MIME parts")


def _header(message: EmailMessage, name: str) -> str:
    try:
        value = message.get(name)
    except Exception:  # noqa: BLE001 - a malformed header must not hide the body
        return ""
    return plain_title(str(value)) if value is not None else ""


def _part_text(part: EmailMessage) -> str:
    try:
        content = part.get_content()
    except Exception:  # noqa: BLE001 - unknown charsets fall back to bytes
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            return ""
        charset = part.get_content_charset() or "utf-8"
        try:
            content = payload.decode(charset, "replace")
        except LookupError:
            content = payload.decode("utf-8", "replace")
    if not isinstance(content, str):
        return ""
    if part.get_content_subtype() == "html":
        return html_visible_text(content)
    return content.replace("\r\n", "\n").replace("\r", "\n")


def parse_eml(raw: bytes) -> tuple[DocumentSection, ...]:
    _check_part_count(raw)
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as error:  # noqa: BLE001 - the parser is lenient but not total
        raise MalformedDocumentError("document is not a readable e-mail message") from error
    if not isinstance(message, EmailMessage):
        raise MalformedDocumentError("document is not a readable e-mail message")
    budget = TextBudget()
    headers = [
        f"{name}: {value}"
        for name in _HEADERS
        if (value := budget.add(_header(message, name)[:MAX_TITLE_CHARS]))
    ]
    try:
        body_part = message.get_body(preferencelist=("plain", "html"))
    except Exception:  # noqa: BLE001
        body_part = None
    body = budget.add(_part_text(body_part).strip()) if body_part is not None else ""
    attachments: list[str] = []
    try:
        for attachment in message.iter_attachments():
            name = attachment.get_filename() or attachment.get_content_type()
            attachments.append(budget.add(plain_title(str(name))[:200]))
            if len(attachments) >= MAX_ATTACHMENT_NAMES:
                break
    except Exception:  # noqa: BLE001 - attachment names are a convenience
        pass
    if not headers and not body:
        raise EmptyDocumentError("message contains no indexable text")
    paragraphs = [part.strip() for part in re.split(r"\n[ \t]*\n", body) if part.strip()]
    check_section_count(len(paragraphs) + 1)
    attachment_line = f"Attachments: {', '.join(attachments)}" if attachments else ""
    envelope = "\n".join(filter(None, ["\n".join(headers), attachment_line]))
    full_text = "\n\n".join(filter(None, [envelope, body]))
    subject = _header(message, "Subject")
    title = subject[:MAX_TITLE_CHARS] or short_title(body) or "Message"
    nested = len(paragraphs) > 1
    sections = [
        DocumentSection(
            stable_key="message",
            title=title,
            kind="email",
            level=1,
            locator=NativeLocator("eml-message", 1, {"part": "message"}),
            search_text=envelope if nested else full_text,
            text=full_text,
        )
    ]
    if nested:
        sections.extend(
            DocumentSection(
                stable_key=f"paragraph:{number}",
                title=short_title(paragraph) or f"Paragraph {number}",
                kind="paragraph",
                level=2,
                locator=NativeLocator("eml-paragraph", 1, {"paragraph": number}),
                search_text=paragraph,
                text=paragraph,
            )
            for number, paragraph in enumerate(paragraphs, start=1)
        )
    return tuple(sections)
