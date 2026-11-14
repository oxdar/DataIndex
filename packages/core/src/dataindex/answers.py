"""Cited answers returned by ``DataIndexClient.chat``."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeGuard

from dataindex._json import locator_json
from dataindex.exceptions import InvalidCitationError
from dataindex.models import EvidenceRecord, JsonValue, NativeLocator, SourceRef
from dataindex.reasoning import ReasoningResult

MAX_EXCERPT_CHARS = 400
_MAX_VERSION_CHARS = 19  # "sha256:" plus 12 hex digits


@dataclass(frozen=True, slots=True)
class Citation:
    """One piece of retrieved evidence that backs part of an answer."""

    number: int
    evidence_id: str
    source_id: str
    source_version: str
    document: str
    operation: str
    evidence_kind: str
    locator: Mapping[str, JsonValue]
    position: str
    node_id: str | None
    excerpt: str
    excerpt_truncated: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "number": self.number,
            "evidence_id": self.evidence_id,
            "source_id": self.source_id,
            "source_version": self.source_version,
            "document": self.document,
            "operation": self.operation,
            "evidence_kind": self.evidence_kind,
            "locator": dict(self.locator),
            "position": self.position,
            "node_id": self.node_id,
            "excerpt": self.excerpt,
            "excerpt_truncated": self.excerpt_truncated,
        }


@dataclass(frozen=True, slots=True)
class AnswerClaim:
    """A sentence of the answer and the citation numbers that support it."""

    text: str
    citations: tuple[int, ...]


class ChatAnswer(str):
    """The rendered answer text, with structured provenance attached.

    ``str(answer)`` is the readable answer with ``[n]`` markers and a Sources
    footer. ``answer.text`` is the body without the footer. Every claim cites
    evidence retrieved through an authorized tool; an answer without a
    supported claim is ``abstained``. ``warnings`` lists documents that were
    skipped, for example because their file changed after it was submitted.
    """

    text: str
    claims: tuple[AnswerClaim, ...]
    citations: tuple[Citation, ...]
    evidence: tuple[EvidenceRecord, ...]
    sources: tuple[SourceRef, ...]
    abstained: bool
    reason: str | None
    tokens_used: int
    depth: int
    warnings: tuple[str, ...]

    def __new__(
        cls,
        rendered: str = "",
        *,
        text: str | None = None,
        claims: tuple[AnswerClaim, ...] = (),
        citations: tuple[Citation, ...] = (),
        evidence: tuple[EvidenceRecord, ...] = (),
        sources: tuple[SourceRef, ...] = (),
        abstained: bool = False,
        reason: str | None = None,
        tokens_used: int = 0,
        depth: int = 0,
        warnings: tuple[str, ...] = (),
    ) -> ChatAnswer:
        self = super().__new__(cls, rendered)
        self.text = rendered if text is None else text
        self.claims = claims
        self.citations = citations
        self.evidence = evidence
        self.sources = sources
        self.abstained = abstained
        self.reason = reason
        self.tokens_used = tokens_used
        self.depth = depth
        self.warnings = tuple(warnings)
        return self

    def __repr__(self) -> str:
        return f"ChatAnswer({str.__repr__(self)})"

    def to_dict(self) -> dict[str, JsonValue]:
        """A JSON-serializable view; raw evidence records are left out."""
        return {
            "text": self.text,
            "abstained": self.abstained,
            "reason": self.reason,
            "claims": [
                {"text": claim.text, "citations": list(claim.citations)}
                for claim in self.claims
            ],
            "citations": [citation.to_dict() for citation in self.citations],
            "sources": [
                {
                    "source_id": source.source_id,
                    "source_type": source.source_type,
                    "version": source.version,
                }
                for source in self.sources
            ],
            "tokens_used": self.tokens_used,
            "depth": self.depth,
            "warnings": list(self.warnings),
        }


def abstained_answer(reason: str, *, warnings: tuple[str, ...] = ()) -> ChatAnswer:
    """An abstention produced without calling a model, for example no documents."""
    return ChatAnswer(
        reason, text=reason, abstained=True, reason=reason, warnings=tuple(warnings)
    )


def build_chat_answer(
    result: ReasoningResult,
    *,
    names: Mapping[str, str] | None = None,
    warnings: tuple[str, ...] = (),
) -> ChatAnswer:
    """Turn a reasoning result into a cited ``ChatAnswer``.

    ``names`` maps source IDs to display names; it only affects presentation.
    """
    names = names or {}
    answer = result.answer
    sources = result.selected_sources
    if answer.abstained or not answer.claims:
        body = _abstention_text(answer.reason)
        return ChatAnswer(
            body,
            text=body,
            evidence=answer.evidence,
            sources=sources,
            abstained=True,
            reason=answer.reason,
            tokens_used=result.tokens_used,
            depth=result.depth,
            warnings=warnings,
        )
    evidence_by_id = {record.execution_id: record for record in answer.evidence}
    numbers: dict[str, int] = {}
    citations: list[Citation] = []
    claims: list[AnswerClaim] = []
    for claim in answer.claims:
        cited: list[int] = []
        for evidence_id in claim.evidence_ids:
            if evidence_id not in numbers:
                record = evidence_by_id.get(evidence_id)
                if record is None:
                    raise InvalidCitationError(
                        "a claim cites evidence that was not retrieved"
                    )
                numbers[evidence_id] = len(numbers) + 1
                citations.append(_citation(numbers[evidence_id], record, names))
            cited.append(numbers[evidence_id])
        claims.append(AnswerClaim(text=claim.text.strip(), citations=tuple(cited)))
    body = "\n".join(
        f"{claim.text} {''.join(f'[{number}]' for number in claim.citations)}"
        for claim in claims
    )
    footer = "\n".join(_footer_line(citation) for citation in citations)
    return ChatAnswer(
        f"{body}\n\nSources:\n{footer}",
        text=body,
        claims=tuple(claims),
        citations=tuple(citations),
        evidence=answer.evidence,
        sources=sources,
        abstained=False,
        reason=None,
        tokens_used=result.tokens_used,
        depth=result.depth,
        warnings=warnings,
    )


def describe_locator(locator: NativeLocator) -> str:
    """A short human-readable position such as ``lines 12-40``."""
    value = locator.value
    start = value.get("line_start")
    end = value.get("line_end")
    if _is_int(start) and _is_int(end):
        return f"line {start}" if start == end else f"lines {start}-{end}"
    query_id = value.get("query_id")
    if isinstance(query_id, str):
        return f"query {query_id}"
    return locator.scheme


def _citation(
    number: int, record: EvidenceRecord, names: Mapping[str, str]
) -> Citation:
    value = record.value
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    )
    truncated = len(text) > MAX_EXCERPT_CHARS
    return Citation(
        number=number,
        evidence_id=record.execution_id,
        source_id=record.source_id,
        source_version=record.source_version,
        document=names.get(record.source_id, record.source_id),
        operation=record.operation,
        evidence_kind=record.evidence_kind.value,
        locator=locator_json(record.locator),
        position=describe_locator(record.locator),
        node_id=record.node_id,
        excerpt=text[:MAX_EXCERPT_CHARS],
        excerpt_truncated=truncated,
    )


def _footer_line(citation: Citation) -> str:
    version = citation.source_version
    if len(version) > _MAX_VERSION_CHARS:
        version = f"{version[:_MAX_VERSION_CHARS]}..."
    return (
        f"[{citation.number}] {citation.document} ({citation.source_id}, "
        f"version {version}) - {citation.position} - {citation.operation} "
        f"({citation.evidence_kind})"
    )


def _abstention_text(reason: str | None) -> str:
    message = "No supported answer could be produced from the selected sources."
    return f"{message} {reason}" if reason else message


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)
