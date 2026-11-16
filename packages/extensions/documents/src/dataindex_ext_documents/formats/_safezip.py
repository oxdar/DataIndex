"""Bounded reading of ZIP containers (OOXML, OpenDocument, EPUB).

A small archive can declare or inflate to an enormous size, so member count,
per-member size and total inflated bytes are all capped, and sizes are measured
while reading rather than trusted from the archive directory.
"""

from __future__ import annotations

import io
import posixpath
import zipfile
import zlib
from urllib.parse import unquote

from dataindex_ext_documents.exceptions import (
    DocumentTooLargeError,
    MalformedDocumentError,
    ProtectedDocumentError,
)
from dataindex_ext_documents.formats._base import ElementBudget

MAX_ZIP_MEMBERS = 10_000
MAX_PART_BYTES = 64 * 1024 * 1024
MAX_TOTAL_INFLATED_BYTES = 256 * 1024 * 1024

_READ_ERRORS = (
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    zlib.error,
    EOFError,
    OSError,
    ValueError,
    NotImplementedError,
    RuntimeError,
)


class SafeZip:
    def __init__(self, raw: bytes, label: str) -> None:
        self.label = label
        try:
            self._zip = zipfile.ZipFile(io.BytesIO(raw))
            infos = self._zip.infolist()
        except _READ_ERRORS as error:
            raise MalformedDocumentError(f"{label} is not a valid ZIP container") from error
        if len(infos) > MAX_ZIP_MEMBERS:
            raise DocumentTooLargeError(
                f"{label} has more than {MAX_ZIP_MEMBERS} archive members"
            )
        self._infos: dict[str, zipfile.ZipInfo] = {}
        self._folded: dict[str, str] = {}
        for info in infos:
            if info.filename in self._infos:
                raise MalformedDocumentError(
                    f"{label} repeats the archive member {info.filename[:200]!r}"
                )
            self._infos[info.filename] = info
            self._folded.setdefault(info.filename.casefold(), info.filename)
        self._inflated = 0
        # Shared by every XML or HTML parse of this package's parts.
        self.elements = ElementBudget()

    def names(self) -> list[str]:
        return list(self._infos)

    def find(self, name: str) -> str | None:
        """Exact member name, matched case-insensitively as OPC part names are."""
        if name in self._infos:
            return name
        return self._folded.get(name.casefold())

    def has(self, name: str) -> bool:
        return self.find(name) is not None

    def read(self, name: str, limit: int = MAX_PART_BYTES) -> bytes:
        member = self.find(name)
        if member is None:
            raise MalformedDocumentError(f"{self.label} has no {name[:200]!r} part")
        info = self._infos[member]
        if info.flag_bits & 0x1:
            raise ProtectedDocumentError(
                f"{self.label} is encrypted; remove the password and submit it again"
            )
        if info.file_size > limit:
            raise DocumentTooLargeError(
                f"{self.label} part {member[:200]!r} exceeds the {limit}-byte limit"
            )
        try:
            with self._zip.open(info) as handle:
                data = handle.read(limit + 1)
        except _READ_ERRORS as error:
            raise MalformedDocumentError(
                f"{self.label} part {member[:200]!r} could not be read"
            ) from error
        if len(data) > limit:
            raise DocumentTooLargeError(
                f"{self.label} part {member[:200]!r} exceeds the {limit}-byte limit"
            )
        self._inflated += len(data)
        if self._inflated > MAX_TOTAL_INFLATED_BYTES:
            raise DocumentTooLargeError(
                f"{self.label} inflates beyond the {MAX_TOTAL_INFLATED_BYTES}-byte limit"
            )
        return data

    def read_optional(self, name: str, limit: int = MAX_PART_BYTES) -> bytes | None:
        return self.read(name, limit) if self.has(name) else None


def resolve_part(base_part: str, target: str) -> str | None:
    """Resolve a relationship or manifest target against the part that names it.

    Returns ``None`` for external URLs and for targets escaping the package root.
    """
    target = unquote(target.split("#", 1)[0].strip())
    if not target or "://" in target or target.startswith(("mailto:", "data:")):
        return None
    if target.startswith("/"):
        joined = target.lstrip("/")
    else:
        joined = posixpath.join(posixpath.dirname(base_part), target)
    normalized = posixpath.normpath(joined)
    if normalized.startswith("..") or normalized in ("", "."):
        return None
    return normalized
