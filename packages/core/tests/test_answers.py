from __future__ import annotations

import copy
import json
import pickle
from collections.abc import Callable

import pytest

from dataindex.answers import (
    MAX_EXCERPT_CHARS,
    AnswerClaim,
    ChatAnswer,
    Citation,
    abstained_answer,
    build_chat_answer,
    describe_locator,
)
from dataindex.exceptions import InvalidCitationError
from dataindex.models import EvidenceKind, EvidenceRecord, JsonValue, NativeLocator, SourceRef
from dataindex.reasoning import GroundedAnswer, GroundedClaim, ReasoningResult

LONG_VERSION = "sha256:" + "a" * 64
DOCUMENT = SourceRef("doc-1", "text/markdown", "v1")
DATABASE = SourceRef("db-1", "application/vnd.sqlite3", LONG_VERSION)
NO_SUPPORT = "No supported answer could be produced from the selected sources."


def lines(start: int, end: int) -> NativeLocator:
    return NativeLocator("md-section", 1, {"line_start": start, "line_end": end})


def make_record(
    execution_id: str,
    *,
    source: SourceRef = DOCUMENT,
    value: JsonValue = "Section text",
    locator: NativeLocator | None = None,
    operation: str = "documents.get_section",
    kind: EvidenceKind = EvidenceKind.OBSERVED,
    node_id: str | None = "n1",
) -> EvidenceRecord:
    return EvidenceRecord(
        source_id=source.source_id,
        source_version=source.version,
        locator=lines(1, 2) if locator is None else locator,
        operation=operation,
        value=value,
        evidence_kind=kind,
        execution_id=execution_id,
        node_id=node_id,
    )


def make_result(
    claims: tuple[GroundedClaim, ...],
    evidence: tuple[EvidenceRecord, ...],
    *,
    abstained: bool = False,
    reason: str | None = None,
    sources: tuple[SourceRef, ...] = (DOCUMENT,),
) -> ReasoningResult:
    return ReasoningResult(
        answer=GroundedAnswer(
            claims=claims, evidence=evidence, abstained=abstained, reason=reason
        ),
        selected_sources=sources,
        tool_calls=(),
        depth=2,
        tokens_used=120,
    )


def single_citation(value: JsonValue) -> Citation:
    record = make_record("e1", value=value)
    result = make_result((GroundedClaim("A claim.", ("e1",)),), (record,))
    (citation,) = build_chat_answer(result).citations
    return citation


def cited_answer() -> ChatAnswer:
    total = make_record(
        "exec-total",
        source=DATABASE,
        value={"total": 42},
        locator=NativeLocator("sql-query", 1, {"query_id": "q-total"}),
        operation="databases.run_query",
        kind=EvidenceKind.CALCULATED,
        node_id=None,
    )
    section = make_record(
        "exec-section",
        value="Refunds are issued within 14 days.",
        locator=lines(12, 40),
    )
    footnote = make_record("exec-footnote", locator=lines(7, 7), node_id="n7")
    unused = make_record("exec-unused", value="never cited by any claim")
    result = make_result(
        (
            GroundedClaim("  The total is 42.  ", ("exec-total", "exec-section")),
            GroundedClaim("Refunds take 14 days.", ("exec-section",)),
            GroundedClaim("See the footnote.", ("exec-footnote",)),
        ),
        (section, footnote, total, unused),
        sources=(DOCUMENT, DATABASE),
    )
    return build_chat_answer(
        result,
        names={"doc-1": "policy.md", "db-1": "Sales DB"},
        warnings=("old.md changed after it was submitted",),
    )


def test_chat_answer_is_a_string_that_carries_provenance() -> None:
    answer = ChatAnswer("Hello [1]", text="Hello", tokens_used=7, depth=3)

    assert isinstance(answer, str)
    assert answer == "Hello [1]"
    assert type(str(answer)) is str
    assert str(answer) == "Hello [1]"
    assert json.dumps(answer) == '"Hello [1]"'
    assert answer.text == "Hello"
    assert (answer.tokens_used, answer.depth) == (7, 3)
    assert repr(answer) == "ChatAnswer('Hello [1]')"


def test_chat_answer_defaults_describe_an_empty_supported_answer() -> None:
    answer = ChatAnswer()

    assert answer == ""
    assert answer.text == ""
    assert answer.claims == answer.citations == answer.evidence == answer.sources == ()
    assert answer.abstained is False
    assert answer.reason is None
    assert (answer.tokens_used, answer.depth) == (0, 0)
    assert answer.warnings == ()


def test_chat_answer_text_defaults_to_the_rendered_text_only_when_omitted() -> None:
    assert ChatAnswer("rendered").text == "rendered"
    assert ChatAnswer("rendered", text="").text == ""


def test_chat_answer_warnings_are_stored_as_a_tuple() -> None:
    answer = ChatAnswer("x", warnings=["skipped a.md"])  # type: ignore[arg-type]

    assert answer.warnings == ("skipped a.md",)


def test_abstained_answer_uses_the_reason_as_its_text() -> None:
    answer = abstained_answer("No documents have been submitted yet.")

    assert answer == "No documents have been submitted yet."
    assert answer.text == answer.reason == "No documents have been submitted yet."
    assert answer.abstained is True
    assert answer.claims == answer.citations == answer.evidence == answer.sources == ()
    assert (answer.tokens_used, answer.depth) == (0, 0)
    assert answer.warnings == ()


def test_abstained_answer_keeps_warnings() -> None:
    answer = abstained_answer("Nothing to search.", warnings=["a.md was skipped"])  # type: ignore[arg-type]

    assert answer.warnings == ("a.md was skipped",)


def test_build_chat_answer_numbers_citations_by_first_use_and_renders_sources() -> None:
    answer = cited_answer()

    assert answer.text == (
        "The total is 42. [1][2]\nRefunds take 14 days. [2]\nSee the footnote. [3]"
    )
    assert answer == answer.text + "\n\nSources:\n" + "\n".join(
        [
            "[1] Sales DB (db-1, version sha256:aaaaaaaaaaaa...) - query q-total"
            " - databases.run_query (calculated)",
            "[2] policy.md (doc-1, version v1) - lines 12-40"
            " - documents.get_section (observed)",
            "[3] policy.md (doc-1, version v1) - line 7"
            " - documents.get_section (observed)",
        ]
    )
    assert answer.claims == (
        AnswerClaim("The total is 42.", (1, 2)),
        AnswerClaim("Refunds take 14 days.", (2,)),
        AnswerClaim("See the footnote.", (3,)),
    )
    assert answer.abstained is False
    assert answer.reason is None
    assert (answer.tokens_used, answer.depth) == (120, 2)
    assert answer.sources == (DOCUMENT, DATABASE)
    assert answer.warnings == ("old.md changed after it was submitted",)


def test_build_chat_answer_cites_each_evidence_record_once() -> None:
    answer = cited_answer()

    assert [citation.number for citation in answer.citations] == [1, 2, 3]
    assert [citation.evidence_id for citation in answer.citations] == [
        "exec-total",
        "exec-section",
        "exec-footnote",
    ]
    # Evidence that no claim cites is retained but never presented as support.
    assert len(answer.evidence) == 4
    assert "exec-unused" not in str(answer)
    assert "never cited" not in str(answer)


def test_build_chat_answer_citations_carry_source_version_and_native_locator() -> None:
    total, section, footnote = cited_answer().citations

    assert total == Citation(
        number=1,
        evidence_id="exec-total",
        source_id="db-1",
        source_version=LONG_VERSION,
        document="Sales DB",
        operation="databases.run_query",
        evidence_kind="calculated",
        locator={"scheme": "sql-query", "version": 1, "value": {"query_id": "q-total"}},
        position="query q-total",
        node_id=None,
        excerpt='{"total":42}',
        excerpt_truncated=False,
    )
    assert section.locator == {
        "scheme": "md-section",
        "version": 1,
        "value": {"line_start": 12, "line_end": 40},
    }
    assert section.excerpt == "Refunds are issued within 14 days."
    assert section.node_id == "n1"
    assert footnote.node_id == "n7"


def test_build_chat_answer_falls_back_to_source_ids_without_display_names() -> None:
    record = make_record("e1")
    result = make_result((GroundedClaim("A claim.", ("e1",)),), (record,))

    unnamed = build_chat_answer(result)
    unrelated = build_chat_answer(result, names={"other": "other.md"})

    for answer in (unnamed, unrelated):
        assert answer.citations[0].document == "doc-1"
        assert "[1] doc-1 (doc-1, version v1)" in answer


@pytest.mark.parametrize(
    ("version", "shown"),
    [
        ("v" * 19, "v" * 19),
        ("v" * 20, "v" * 19 + "..."),
        (LONG_VERSION, "sha256:aaaaaaaaaaaa..."),
    ],
)
def test_build_chat_answer_shortens_only_long_versions_in_the_footer(
    version: str, shown: str
) -> None:
    source = SourceRef("doc-1", "text/markdown", version)
    record = make_record("e1", source=source)
    result = make_result((GroundedClaim("A claim.", ("e1",)),), (record,), sources=(source,))

    answer = build_chat_answer(result)

    assert f"version {shown})" in answer
    assert answer.citations[0].source_version == version


@pytest.mark.parametrize(
    ("length", "truncated"), [(1, False), (399, False), (400, False), (401, True)]
)
def test_build_chat_answer_clips_excerpts_at_the_limit_and_flags_it(
    length: int, truncated: bool
) -> None:
    value = "x" * length

    citation = single_citation(value)

    assert citation.excerpt == value[:MAX_EXCERPT_CHARS]
    assert len(citation.excerpt) == min(length, MAX_EXCERPT_CHARS)
    assert citation.excerpt_truncated is truncated


def test_build_chat_answer_never_places_bulk_structured_values_in_an_excerpt() -> None:
    citation = single_citation({"rows": list(range(500))})

    assert len(citation.excerpt) == MAX_EXCERPT_CHARS
    assert citation.excerpt_truncated is True


@pytest.mark.parametrize(
    ("value", "excerpt"),
    [
        ({"city": "Zürich", "rows": [1, 2]}, '{"city":"Zürich","rows":[1,2]}'),
        ([1, 2], "[1,2]"),
        (42, "42"),
        (1.5, "1.5"),
        (True, "true"),
        (None, "null"),
        ("plain text", "plain text"),
    ],
)
def test_build_chat_answer_renders_values_as_text_or_compact_json(
    value: JsonValue, excerpt: str
) -> None:
    assert single_citation(value).excerpt == excerpt


def test_build_chat_answer_abstains_with_the_engines_reason() -> None:
    evidence = (make_record("e1"),)
    result = make_result((), evidence, abstained=True, reason="Evidence was insufficient.")

    answer = build_chat_answer(result, warnings=("a.md changed after it was submitted",))

    assert answer == f"{NO_SUPPORT} Evidence was insufficient."
    assert answer.text == str(answer)
    assert answer.abstained is True
    assert answer.reason == "Evidence was insufficient."
    assert answer.claims == answer.citations == ()
    assert answer.sources == (DOCUMENT,)
    assert (answer.tokens_used, answer.depth) == (120, 2)
    assert answer.warnings == ("a.md changed after it was submitted",)


def test_build_chat_answer_abstains_without_a_reason() -> None:
    answer = build_chat_answer(make_result((), (), abstained=True))

    assert answer == NO_SUPPORT
    assert answer.reason is None
    assert answer.abstained is True


def test_build_chat_answer_abstains_when_nothing_was_claimed() -> None:
    answer = build_chat_answer(make_result((), (make_record("e1"),), abstained=False))

    assert answer.abstained is True
    assert answer == NO_SUPPORT
    assert answer.citations == ()


def test_build_chat_answer_never_renders_claims_of_an_abstained_result() -> None:
    result = make_result(
        (GroundedClaim("A claim that must stay hidden.", ("e1",)),),
        (make_record("e1"),),
        abstained=True,
        reason="Not enough support.",
    )

    answer = build_chat_answer(result)

    assert "must stay hidden" not in answer
    assert "must stay hidden" not in json.dumps(answer.to_dict())
    assert answer.claims == answer.citations == ()
    assert answer.abstained is True


def test_build_chat_answer_fails_closed_on_a_claim_citing_unknown_evidence() -> None:
    result = make_result(
        (GroundedClaim("An unsupported claim.", ("missing",)),), (make_record("e1"),)
    )

    with pytest.raises(InvalidCitationError, match="was not retrieved"):
        build_chat_answer(result)


def test_to_dict_is_a_json_serializable_view_without_raw_evidence() -> None:
    answer = cited_answer()

    payload = answer.to_dict()

    assert json.loads(json.dumps(payload)) == payload
    assert set(payload) == {
        "text",
        "abstained",
        "reason",
        "claims",
        "citations",
        "sources",
        "tokens_used",
        "depth",
        "warnings",
    }
    assert payload["text"] == answer.text
    assert payload["abstained"] is False
    assert payload["reason"] is None
    assert payload["claims"][0] == {"text": "The total is 42.", "citations": [1, 2]}  # type: ignore[index]
    assert payload["citations"][0] == answer.citations[0].to_dict()  # type: ignore[index]
    assert payload["sources"] == [
        {"source_id": "doc-1", "source_type": "text/markdown", "version": "v1"},
        {
            "source_id": "db-1",
            "source_type": "application/vnd.sqlite3",
            "version": LONG_VERSION,
        },
    ]
    assert payload["warnings"] == ["old.md changed after it was submitted"]
    assert (payload["tokens_used"], payload["depth"]) == (120, 2)


def test_to_dict_of_an_abstained_answer_is_serializable() -> None:
    payload = abstained_answer("No documents.", warnings=("w",)).to_dict()

    assert json.loads(json.dumps(payload)) == payload
    assert payload["abstained"] is True
    assert payload["reason"] == "No documents."
    assert payload["claims"] == []
    assert payload["citations"] == []
    assert payload["sources"] == []


@pytest.mark.parametrize(
    "clone",
    [
        copy.copy,
        copy.deepcopy,
        lambda value: pickle.loads(pickle.dumps(value)),
    ],
    ids=["copy", "deepcopy", "pickle"],
)
def test_chat_answer_survives_copying_and_pickling(
    clone: Callable[[ChatAnswer], ChatAnswer],
) -> None:
    original = cited_answer()

    duplicate = clone(original)

    assert type(duplicate) is ChatAnswer
    assert duplicate == original
    assert duplicate.text == original.text != str(original)
    assert duplicate.claims == original.claims
    assert duplicate.citations == original.citations
    assert duplicate.evidence == original.evidence
    assert duplicate.sources == original.sources
    assert duplicate.warnings == original.warnings
    assert duplicate.to_dict() == original.to_dict()


@pytest.mark.parametrize(
    "clone",
    [copy.copy, copy.deepcopy, lambda value: pickle.loads(pickle.dumps(value))],
    ids=["copy", "deepcopy", "pickle"],
)
def test_abstained_answer_survives_copying_and_pickling(
    clone: Callable[[ChatAnswer], ChatAnswer],
) -> None:
    original = abstained_answer("Nothing to search.", warnings=("w",))

    duplicate = clone(original)

    assert duplicate == original
    assert duplicate.abstained is True
    assert duplicate.reason == "Nothing to search."
    assert duplicate.warnings == ("w",)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"line_start": 5, "line_end": 5}, "line 5"),
        ({"line_start": 12, "line_end": 40}, "lines 12-40"),
        ({"line_start": 1, "line_end": 2, "query_id": "q"}, "lines 1-2"),
        ({"query_id": "q1"}, "query q1"),
        ({"query_id": 7}, "custom-scheme"),
        ({"line_start": 1}, "custom-scheme"),
        ({"line_start": True, "line_end": True}, "custom-scheme"),
        ({"line_start": "1", "line_end": "2"}, "custom-scheme"),
        ({}, "custom-scheme"),
    ],
)
def test_describe_locator_prefers_lines_then_queries_then_the_scheme(
    value: dict[str, JsonValue], expected: str
) -> None:
    assert describe_locator(NativeLocator("custom-scheme", 1, value)) == expected
