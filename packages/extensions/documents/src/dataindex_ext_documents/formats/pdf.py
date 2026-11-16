"""Reader for PDF documents, using the optional ``pypdf`` dependency.

Pages are the native unit. When the PDF has an outline (bookmarks), outline
entries become parent sections over the pages they start on. Text is extracted
from the PDF's text layer only: scanned pages without one are not OCRed.

pypdf parses content streams and rebuilds fonts in pure Python for every page
and every form XObject invocation, so a small file can demand a large amount of
work. :class:`_WorkBudget` charges that work deterministically before pypdf
does it, in units of about one byte of dense content stream.

pypdf also keeps every decoded stream for the life of the reader and retries a
stream that failed to decode each time it is used. While a PDF is read, every
decode goes through :meth:`_WorkBudget.decode`, which bounds the total decoded
bytes and the number of damaged streams and fails a damaged stream again
without decoding it. The hook replaces ``pypdf.filters.decode_stream_data`` once
per process and is inert outside :func:`_read_pages`.
"""

from __future__ import annotations

import functools
import io
import logging
import re
import threading
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from dataindex.models import NativeLocator
from dataindex_ext_documents.exceptions import (
    DocumentReaderUnavailableError,
    DocumentTooLargeError,
    DocumentIndexError,
    EmptyDocumentError,
    MalformedDocumentError,
    ProtectedDocumentError,
)
from dataindex_ext_documents.formats._base import (
    MAX_TITLE_CHARS,
    DocumentSection,
    TextBudget,
    check_section_count,
    plain_title,
    short_title,
)

MAX_PDF_PAGES = 2_000
MAX_OUTLINE_ENTRIES = 2_000
MAX_OUTLINE_DEPTH = 6
# Text-extraction work allowed for one PDF: 4 MiB plus 16 units per byte of
# file, at most 32 MiB. A typical exported document needs about 6 units per byte.
PDF_WORK_BASE = 4 * 1024 * 1024
PDF_WORK_PER_FILE_BYTE = 16
MAX_PDF_WORK = 32 * 1024 * 1024
MAX_PDF_STREAM_BYTES = 32 * 1024 * 1024
MAX_PDF_DECODED_BYTES = 128 * 1024 * 1024
MAX_PDF_DAMAGED_STREAMS = 8
# pypdf counts outline entries itself and copies a visited set for every nested
# outline, so a long outline costs quadratic time before it can be truncated.
_PYPDF_OUTLINE_ENTRIES = 5 * MAX_OUTLINE_ENTRIES
_PAGE_WORK = 64
_FONT_WORK = 64
# pypdf caps one ToUnicode map at 100,000 entries and one /W array at 65,536 widths.
_MAX_CMAP_ENTRIES = 100_000
_MAX_CID_WIDTHS = 65_536
# pypdf parses a content stream only if one of these operators occurs in it;
# otherwise it only scans the bytes, which costs about an eighth as much.
_TEXT_OPERATOR = re.compile(rb"T[jJ]|Do|['\"]")
_BFRANGE_BLOCK = re.compile(rb"beginbfrange(.*?)endbfrange", re.DOTALL)
_BFRANGE_LINE = re.compile(rb"(?m)^[ \t]*<([0-9A-Fa-f]{1,16})>[ \t]*<([0-9A-Fa-f]{1,16})>")
_INSTALL_HINT = 'install it with: pip install "dataindex-ext-documents[pdf]"'

# pypdf logs recoverable damage as warnings. Without any handler they reach stderr
# through logging's last-resort handler; applications that configure logging still
# receive them.
logging.getLogger("pypdf").addHandler(logging.NullHandler())

_ACTIVE_WORK: ContextVar[_WorkBudget | None] = ContextVar("dataindex_pdf_work", default=None)
_GUARD_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class _Entry:
    ordinal: int
    level: int
    title: str
    page: int


def _clean(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _resolve(value: Any) -> Any:
    try:
        return value.get_object()
    except DocumentIndexError:
        raise
    except Exception:  # noqa: BLE001 - pypdf reports broken references itself
        return None


def _number(value: Any) -> int | None:
    resolved = _resolve(value)
    if isinstance(resolved, bool) or not isinstance(resolved, (int, float)):
        return None
    try:
        return int(resolved)
    except (OverflowError, ValueError):
        return None


class _WorkBudget:
    """Deterministic bound on pypdf text-extraction work for one document.

    One unit is about the cost of parsing one byte of dense content stream.
    Content streams cost their decoded size, or an eighth of it when they hold
    no text operator. A font costs a base amount plus its ToUnicode entries and
    bytes, /W widths, /Differences, Type 3 glyph procedures and Type 1 font
    file, because pypdf rebuilds every font of a page or form each time it
    extracts that page or form. Weights are computed once per object.

    The first limit reached is kept in :attr:`failure`. pypdf swallows some
    exceptions, so callers raise it with :meth:`check` after each step.
    """

    __slots__ = (
        "limit",
        "used",
        "decoded",
        "failure",
        "_damaged",
        "_streams",
        "_font_sets",
        "_fonts",
        "_cmaps",
        "_cids",
    )

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self.decoded = 0
        self.failure: DocumentIndexError | None = None
        self._damaged: dict[int, tuple[Any, type[BaseException], tuple[Any, ...]]] = {}
        self._streams: dict[int, tuple[Any, int, bool, bytes, bytes]] = {}
        self._font_sets: dict[int, tuple[Any, int]] = {}
        self._fonts: dict[int, tuple[Any, int]] = {}
        self._cmaps: dict[int, tuple[Any, int]] = {}
        self._cids: dict[int, tuple[Any, int]] = {}

    def charge(self, units: int) -> bool:
        self.used += units
        if self.used > self.limit:
            self._fail(
                DocumentTooLargeError(
                    "PDF is too costly to extract: its page content, repeated forms and fonts "
                    "exceed the work limit for a file of this size; split it into smaller PDFs "
                    "and submit them again"
                )
            )
        return self.failure is None

    def check(self) -> None:
        if self.failure is not None:
            raise self.failure

    def _fail(self, error: DocumentIndexError) -> DocumentIndexError:
        if self.failure is None:
            self.failure = error
        # A fresh instance per raise, so tracebacks do not accumulate on one object.
        return type(self.failure)(str(self.failure))

    def decode(
        self,
        stream: Any,
        decode: Callable[[Any], bytes],
        limit_error: type[Exception],
    ) -> bytes:
        if self.failure is not None:
            raise type(self.failure)(str(self.failure))
        damaged = self._damaged.get(id(stream))
        if damaged is not None:
            raise _recreate(damaged[1], damaged[2])
        try:
            data = decode(stream)
        except DocumentIndexError:
            raise
        except limit_error as error:
            raise self._fail(
                DocumentTooLargeError(f"PDF has a stream beyond a decoding limit: {str(error)[:200]}")
            ) from error
        except Exception as error:
            self._damaged[id(stream)] = (stream, type(error), error.args)
            if len(self._damaged) > MAX_PDF_DAMAGED_STREAMS:
                raise self._fail(
                    MalformedDocumentError(
                        f"PDF has more than {MAX_PDF_DAMAGED_STREAMS} streams that cannot be decoded"
                    )
                ) from error
            raise
        self.decoded += len(data)
        if self.decoded > MAX_PDF_DECODED_BYTES:
            raise self._fail(
                DocumentTooLargeError(
                    f"PDF streams decode to more than {MAX_PDF_DECODED_BYTES // (1024 * 1024)} MiB; "
                    "split it into smaller PDFs and submit them again"
                )
            )
        return data

    def page(self, page: Any) -> int:
        contents = _resolve(page.get("/Contents"))
        parts = contents if isinstance(contents, list) else [contents]
        size = 0
        has_text = False
        previous_tail = b""
        for part in parts:
            length, part_has_text, head, tail = self._stream(part)
            size += length
            has_text = has_text or part_has_text or previous_tail + head in (b"Tj", b"TJ", b"Do")
            previous_tail = tail
            if size > self.limit:
                break
        return _PAGE_WORK + (size if has_text else size // 8) + self._font_set(page)

    def form(self, xform: Any) -> int:
        length, has_text, _, _ = self._stream(xform)
        return _PAGE_WORK + (length if has_text else length // 8) + self._font_set(xform)

    def damaged(self, value: Any) -> bool:
        return id(_resolve(value)) in self._damaged

    def _stream(self, value: Any) -> tuple[int, bool, bytes, bytes]:
        stream = _resolve(value)
        cached = self._streams.get(id(stream))
        if cached is None:
            data = _stream_bytes(stream)
            has_text = _TEXT_OPERATOR.search(data) is not None
            cached = (stream, len(data), has_text, data[:1], data[-1:])
            self._streams[id(stream)] = cached
        return cached[1], cached[2], cached[3], cached[4]

    def _font_set(self, owner: Any) -> int:
        try:
            resources = _resolve(owner.get_inherited("/Resources", None))
        except Exception:  # noqa: BLE001 - pypdf treats malformed resources as none
            return 0
        if not isinstance(resources, dict):
            return 0
        fonts = _resolve(resources.get("/Font"))
        if not isinstance(fonts, dict):
            return 0
        cached = self._font_sets.get(id(fonts))
        if cached is not None:
            return cached[1]
        total = 0
        for value in fonts.values():
            total += self._font(value)
            if total > self.limit:
                return total
        self._font_sets[id(fonts)] = (fonts, total)
        return total

    def _font(self, value: Any) -> int:
        font = _resolve(value)
        if not isinstance(font, dict):
            return 1
        cached = self._fonts.get(id(font))
        if cached is not None:
            return cached[1]
        weight = _FONT_WORK
        # Mirrors pypdf's _cmap.build_char_map_from_dict: it decodes /ToUnicode when
        # the key is present, otherwise the embedded file of a Type 1 font.
        if "/ToUnicode" in font:
            to_unicode = _resolve(font.get("/ToUnicode"))
            if hasattr(to_unicode, "get_data"):
                weight += self._cmap(to_unicode)
        elif font.get("/Subtype") == "/Type1":
            descriptor = _resolve(font.get("/FontDescriptor"))
            if isinstance(descriptor, dict):
                weight += _type1_font_file_size(descriptor) // 512
        encoding = _resolve(font.get("/Encoding"))
        if isinstance(encoding, dict):
            weight += _length(encoding.get("/Differences")) // 8
        weight += _length(font.get("/Widths")) // 8 + _length(font.get("/CharProcs")) // 8
        descendants = _resolve(font.get("/DescendantFonts"))
        if isinstance(descendants, list):
            for descendant in descendants:
                weight += self._cid_font(descendant)
                if weight > self.limit:
                    break
        self._fonts[id(font)] = (font, weight)
        return weight

    def _cmap(self, stream: Any) -> int:
        cached = self._cmaps.get(id(stream))
        if cached is not None:
            return cached[1]
        data = _stream_bytes(stream)
        entries = data.count(b"<") // 2
        lines = 0
        for block in _BFRANGE_BLOCK.finditer(data):
            for match in _BFRANGE_LINE.finditer(block[1]):
                low, high = int(match[1], 16), int(match[2], 16)
                entries += max(0, min(high - low + 1, _MAX_CMAP_ENTRIES))
                lines += 1
                if entries >= _MAX_CMAP_ENTRIES or lines >= _MAX_CMAP_ENTRIES:
                    break
            if entries >= _MAX_CMAP_ENTRIES or lines >= _MAX_CMAP_ENTRIES:
                break
        weight = 2 * min(entries, _MAX_CMAP_ENTRIES) + len(data) // 4
        self._cmaps[id(stream)] = (stream, weight)
        return weight

    def _cid_font(self, value: Any) -> int:
        descendant = _resolve(value)
        if not isinstance(descendant, dict):
            return 1
        cached = self._cids.get(id(descendant))
        if cached is not None:
            return cached[1]
        weight = 1
        widths = _resolve(descendant.get("/W"))
        if isinstance(widths, list):
            covered = 0
            index = 0
            count = len(widths)
            while index < count and covered < _MAX_CID_WIDTHS:
                following = _resolve(widths[index + 1]) if index + 1 < count else None
                if isinstance(following, list):
                    covered += len(following)
                    index += 2
                    continue
                first, last = _number(widths[index]), _number(following)
                if first is not None and last is not None and index + 2 < count:
                    covered += max(0, min(last - first + 1, _MAX_CID_WIDTHS))
                    index += 3
                    continue
                index += 1
            weight += count + min(covered, _MAX_CID_WIDTHS) // 8
        self._cids[id(descendant)] = (descendant, weight)
        return weight


def _stream_bytes(value: Any) -> bytes:
    stream = _resolve(value)
    try:
        data = stream.get_data()
    except DocumentIndexError:
        raise
    except Exception:  # noqa: BLE001 - pypdf reports undecodable streams itself
        return b""
    return data if isinstance(data, bytes) else b""


def _type1_font_file_size(descriptor: dict[Any, Any]) -> int:
    for key in ("/FontFile", "/FontFile3"):
        font_file = _resolve(descriptor.get(key))
        if not hasattr(font_file, "get_data"):
            continue
        if key == "/FontFile3" and font_file.get("/Subtype") != "/Type1C":
            continue
        return len(_stream_bytes(font_file))
    return 0


def _recreate(kind: type[BaseException], args: tuple[Any, ...]) -> BaseException:
    try:
        return kind(*args)
    except Exception:  # noqa: BLE001 - some exception types need other arguments
        return RuntimeError(f"PDF stream could not be decoded: {kind.__name__}")


def _install_decode_guard(filters: Any, limit_error: type[Exception]) -> None:
    """Route pypdf stream decoding through the active :class:`_WorkBudget`.

    ``EncodedStreamObject.get_data`` looks ``decode_stream_data`` up on
    ``pypdf.filters`` at call time, so replacing the module attribute covers
    every stream pypdf decodes. Without an active budget the original runs.
    """
    with _GUARD_LOCK:
        original = filters.decode_stream_data
        if getattr(original, "_dataindex_guard", False):
            return

        @functools.wraps(original)
        def decode_stream_data(stream: Any) -> bytes:
            work = _ACTIVE_WORK.get()
            if work is None:
                return original(stream)
            return work.decode(stream, original, limit_error)

        decode_stream_data._dataindex_guard = True  # type: ignore[attr-defined]
        filters.decode_stream_data = decode_stream_data


def _length(value: Any) -> int:
    resolved = _resolve(value)
    return len(resolved) if isinstance(resolved, (list, dict)) else 0


def _guard_forms(page: Any, work: _WorkBudget) -> None:
    """Charge every form XObject that pypdf extracts while it reads ``page``.

    pypdf calls ``self.extract_xform_text`` for each ``Do`` of a form, including
    forms nested in forms, and parses the form again each time. This instance
    attribute intercepts those calls for this page only. pypdf catches
    exceptions raised there, so an over-budget form is skipped instead and the
    caller runs :meth:`_WorkBudget.check` after the page.
    """
    extract: Callable[..., str] = page.extract_xform_text

    def extract_xform_text(xform: Any, *args: Any, **kwargs: Any) -> str:
        if work.failure is not None or not work.charge(work.form(xform)) or work.damaged(xform):
            return ""
        return extract(xform, *args, **kwargs)

    page.extract_xform_text = extract_xform_text


def _outline(reader: object, page_count: int) -> list[_Entry]:
    entries: list[_Entry] = []

    def walk(items: object, level: int) -> None:
        if not isinstance(items, list) or level > 64:
            return
        for item in items:
            if len(entries) >= MAX_OUTLINE_ENTRIES:
                return
            if isinstance(item, list):
                walk(item, level + 1)
                continue
            try:
                page = reader.get_destination_page_number(item)  # type: ignore[attr-defined]
                title = plain_title(str(item.title))[:MAX_TITLE_CHARS]
            except Exception:  # noqa: BLE001 - a broken bookmark is skipped, not fatal
                continue
            if page is None or not 0 <= page < page_count or not title:
                continue
            entries.append(
                _Entry(len(entries) + 1, min(level, MAX_OUTLINE_DEPTH), title, page + 1)
            )

    try:
        walk(reader.outline, 1)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - outlines are optional navigation aids
        return []
    return sorted(entries, key=lambda entry: entry.page)


def _read_pages(raw: bytes) -> tuple[list[str], list[_Entry]]:
    try:
        import pypdf.filters
        from pypdf import PdfReader, apply_configuration
        from pypdf.errors import DependencyError, LimitReachedError
    except ImportError as error:
        raise DocumentReaderUnavailableError(
            f"PDF support needs the optional pypdf package; {_INSTALL_HINT}"
        ) from error
    _install_decode_guard(pypdf.filters, LimitReachedError)
    work = _WorkBudget(min(MAX_PDF_WORK, PDF_WORK_BASE + PDF_WORK_PER_FILE_BYTE * len(raw)))
    token = _ACTIVE_WORK.set(work)
    try:
        # Shared /Kids nodes are visited once per reference, so this bounds the
        # page-tree walk before the page count can be checked.
        with apply_configuration(
            page_tree_maximum_entries=4 * MAX_PDF_PAGES,
            outline_maximum_entries=_PYPDF_OUTLINE_ENTRIES,
            zlib_maximum_output_length=MAX_PDF_STREAM_BYTES,
            lzw_maximum_output_length=MAX_PDF_STREAM_BYTES,
            run_length_maximum_output_length=MAX_PDF_STREAM_BYTES,
            brotli_maximum_output_length=MAX_PDF_STREAM_BYTES,
            array_based_stream_maximum_output_length=MAX_PDF_STREAM_BYTES,
            stream_decoding_work_maximum_length=4 * MAX_PDF_STREAM_BYTES,
        ):
            reader = PdfReader(io.BytesIO(raw), strict=False)
            if reader.is_encrypted:
                try:
                    decrypted = reader.decrypt("")
                except DependencyError as error:
                    raise ProtectedDocumentError(
                        "PDF is encrypted with a cipher that needs extra libraries; "
                        "remove the encryption and submit it again"
                    ) from error
                if not decrypted:
                    raise ProtectedDocumentError(
                        "PDF is password protected; remove the password and submit it again"
                    )
            page_count = len(reader.pages)
            work.check()
            if page_count > MAX_PDF_PAGES:
                raise DocumentTooLargeError(f"PDF has more than {MAX_PDF_PAGES} pages")
            budget = TextBudget()
            pages: list[str] = []
            for page in reader.pages:
                work.charge(work.page(page))
                work.check()
                _guard_forms(page, work)
                text = page.extract_text() or ""
                work.check()
                pages.append(budget.add(_clean(text)))
            entries = _outline(reader, page_count)
            work.check()
    except DocumentIndexError:
        raise
    except LimitReachedError as error:
        raise DocumentTooLargeError(f"PDF exceeds a parser limit: {str(error)[:200]}") from error
    except Exception as error:  # noqa: BLE001 - pypdf raises many unrelated types
        if work.failure is not None:
            raise work.failure from error
        raise MalformedDocumentError(f"PDF could not be read: {type(error).__name__}") from error
    finally:
        _ACTIVE_WORK.reset(token)
    return pages, entries


def parse_pdf(raw: bytes) -> tuple[DocumentSection, ...]:
    if b"%PDF-" not in raw[:1024]:
        raise MalformedDocumentError("document is not a PDF: it has no %PDF- header")
    pages, entries = _read_pages(raw)
    if not any(pages):
        raise EmptyDocumentError(
            "PDF has no extractable text layer; scanned pages are not OCRed"
        )
    last_page = len(pages)
    check_section_count(len(entries) + sum(1 for text in pages if text))
    sections: list[DocumentSection] = []
    by_page: dict[int, list[_Entry]] = {}
    page_ends: dict[int, int] = {}
    for index, entry in enumerate(entries):
        by_page.setdefault(entry.page, []).append(entry)
        page_ends[entry.ordinal] = last_page
        for later in entries[index + 1 :]:
            if later.level <= entry.level:
                page_ends[entry.ordinal] = max(entry.page, later.page - 1)
                break
    current_level = 0
    for page_number in range(1, last_page + 1):
        for entry in by_page.get(page_number, ()):
            page_end = page_ends[entry.ordinal]
            body = "\n\n".join(
                text for text in pages[entry.page - 1 : page_end] if text
            )
            sections.append(
                DocumentSection(
                    stable_key=f"outline:{entry.ordinal}",
                    title=entry.title,
                    kind="outline",
                    level=entry.level,
                    locator=NativeLocator(
                        "pdf-outline",
                        1,
                        {
                            "ordinal": entry.ordinal,
                            "title": entry.title,
                            "page_start": entry.page,
                            "page_end": page_end,
                        },
                    ),
                    search_text=entry.title,
                    text=f"{entry.title}\n\n{body}" if body else entry.title,
                )
            )
            current_level = entry.level
        text = pages[page_number - 1]
        if not text:
            continue
        heading = short_title(text)
        sections.append(
            DocumentSection(
                stable_key=f"page:{page_number}",
                title=f"Page {page_number}: {heading}" if heading else f"Page {page_number}",
                kind="page",
                level=current_level + 1,
                locator=NativeLocator("pdf-page", 1, {"page": page_number}),
                search_text=text,
                text=text,
            )
        )
    return tuple(sections)
