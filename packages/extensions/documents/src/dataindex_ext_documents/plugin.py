"""Free, in-memory DocumentIndex extension for DataIndex."""

from __future__ import annotations

import re
import time
import uuid
from collections import Counter
from collections.abc import Iterator, Sequence

from dataindex.models import (
    DataIndexExtension,
    EvidenceKind,
    EvidenceRecord,
    ExtensionManifest,
    HealthResult,
    HealthStatus,
    IndexContext,
    NativeLocator,
    Node,
    ProbeResult,
    RetrievalContext,
    RetrievalToolSpec,
    SourceRef,
    ToolRequest,
)

from dataindex_ext_documents.exceptions import (
    ClosedDocumentIndexError,
    DocumentDeadlineExceededError,
    DocumentNotRegisteredError,
    DocumentReaderUnavailableError,
    DocumentSectionNotFoundError,
    DocumentSourceConflictError,
    InvalidDocumentToolRequestError,
    NoDocumentMatchError,
    UnsupportedDocumentFormatError,
)
from dataindex_ext_documents.formats import FORMATS_BY_SOURCE_TYPE, SOURCE_TYPES
from dataindex_ext_documents.readers import (
    MAX_QUERY_LENGTH,
    IndexedDocument,
    locator_key,
    parse_document,
)

PLUGIN_ID = "documents"
DISTRIBUTION = "dataindex-ext-documents"
PLUGIN_VERSION = "0.1.0"
CORE_API_RANGE = ">=0.1,<0.2"
RETRIEVE_TOOL_ID = "documents.search"
SECTION_TOOL_ID = "documents.get_section"
MAX_NODE_ID_LENGTH = 128

_MANIFEST = ExtensionManifest(
    plugin_id=PLUGIN_ID,
    distribution=DISTRIBUTION,
    plugin_version=PLUGIN_VERSION,
    core_api_range=CORE_API_RANGE,
    edition="free",
    license_mode="none",
    capabilities=("document.index", "document.retrieve"),
    source_types=SOURCE_TYPES,
    publisher="DataIndex project",
)

_TOOL = RetrievalToolSpec(
    tool_id=RETRIEVE_TOOL_ID,
    input_schema={
        "type": "object",
        "description": (
            "Keyword search over the sections of one document. Returns the full "
            "text of the single best-matching section."
        ),
        "properties": {
            "source_id": {"type": "string", "minLength": 1},
            "source_version": {"type": "string", "minLength": 1},
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_QUERY_LENGTH,
                "description": "Words that appear in the wanted section.",
            },
        },
        "required": ["source_id", "source_version", "query"],
        "additionalProperties": False,
    },
    capability="document.retrieve",
    maximum_results=1,
)

_SECTION_TOOL = RetrievalToolSpec(
    tool_id=SECTION_TOOL_ID,
    input_schema={
        "type": "object",
        "description": (
            "Read the full text of one section of one document, chosen by the "
            "node_id of an entry in the document outline."
        ),
        "properties": {
            "source_id": {"type": "string", "minLength": 1},
            "source_version": {"type": "string", "minLength": 1},
            "node_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_NODE_ID_LENGTH,
                "description": "A node_id copied from the document outline.",
            },
        },
        "required": ["source_id", "source_version", "node_id"],
        "additionalProperties": False,
    },
    capability="document.retrieve",
    maximum_results=1,
)


class DocumentIndexExtension(DataIndexExtension):
    """Index explicit content bytes and retrieve exact natural sections.

    The host reads only authorized sources and passes their bytes to
    ``register_document``. This extension never interprets source IDs as paths
    and performs no filesystem or network access. Formats that need an optional
    parser (PDF and the 97-2003 Office formats) are reported as unsupported by
    ``probe`` until that parser is installed.
    """

    def __init__(self) -> None:
        self._documents: dict[tuple[str, str], IndexedDocument] = {}
        self._closed = False

    def manifest(self) -> ExtensionManifest:
        return _MANIFEST

    def probe(self, source: SourceRef) -> ProbeResult:
        self._ensure_open()
        spec = FORMATS_BY_SOURCE_TYPE.get(source.source_type)
        if spec is None:
            return ProbeResult(
                False, f"unsupported document source type {source.source_type!r}"
            )
        missing = spec.missing_modules()
        if missing:
            return ProbeResult(
                False,
                f"{spec.label} files need the optional {', '.join(missing)} "
                f"package; install it with: {spec.install_hint()}",
            )
        return ProbeResult(True)

    def register_document(self, source: SourceRef, content: bytes | str) -> None:
        self._ensure_open()
        probe = self.probe(source)
        if not probe.supported:
            error = (
                DocumentReaderUnavailableError
                if source.source_type in FORMATS_BY_SOURCE_TYPE
                else UnsupportedDocumentFormatError
            )
            raise error(probe.reason or "unsupported format")
        parsed = parse_document(source, content)
        key = (source.source_id, source.version)
        existing = self._documents.get(key)
        if existing is not None:
            if (
                existing.digest != parsed.digest
                or existing.source.source_type != source.source_type
            ):
                raise DocumentSourceConflictError(
                    "the same source ID/version was registered with different content"
                )
            return
        previous_revisions = [
            old_key
            for old_key, old_document in self._documents.items()
            if old_key[0] == source.source_id
        ]
        if any(
            self._documents[old_key].source.source_type != source.source_type
            for old_key in previous_revisions
        ):
            raise DocumentSourceConflictError(
                "a source's type cannot change between registered revisions"
            )
        for old_key in previous_revisions:
            del self._documents[old_key]
        self._documents[key] = parsed

    def unregister_document(self, source_id: str) -> int:
        """Drop every registered revision of a source; return how many were dropped."""
        self._ensure_open()
        keys = [key for key in self._documents if key[0] == source_id]
        for key in keys:
            del self._documents[key]
        return len(keys)

    def iter_index_records(
        self, source: SourceRef, ctx: IndexContext
    ) -> Iterator[Node]:
        self._ensure_open()
        document = self._get_document(source)
        for node in document.nodes:
            self._check_deadline(ctx.deadline_monotonic)
            yield node

    def tools(self) -> Sequence[RetrievalToolSpec]:
        self._ensure_open()
        return (_TOOL, _SECTION_TOOL)

    def tools_for_source(
        self, source: SourceRef
    ) -> Sequence[RetrievalToolSpec]:
        self._ensure_open()
        if (source.source_id, source.version) not in self._documents:
            return ()
        self._get_document(source)
        return (_TOOL, _SECTION_TOOL)

    def run_tool(
        self, request: ToolRequest, ctx: RetrievalContext
    ) -> EvidenceRecord:
        self._ensure_open()
        if request.tool_id == SECTION_TOOL_ID:
            return self._get_section(request, ctx)
        if request.tool_id != RETRIEVE_TOOL_ID:
            raise InvalidDocumentToolRequestError(
                f"unknown document tool {request.tool_id!r}"
            )
        parameters = request.parameters
        required = {"source_id", "source_version", "query"}
        if set(parameters) != required or any(
            not isinstance(parameters[key], str) or not parameters[key].strip()
            for key in required
        ):
            raise InvalidDocumentToolRequestError(
                "documents.search requires non-empty source_id, source_version and query"
            )
        query = parameters["query"].strip()
        if len(query) > MAX_QUERY_LENGTH:
            raise InvalidDocumentToolRequestError(
                f"query must be at most {MAX_QUERY_LENGTH} characters"
            )
        self._check_deadline(ctx.deadline_monotonic)
        source = self._source_for(
            parameters["source_id"], parameters["source_version"]
        )
        document = self._get_document(source)
        terms = tuple(
            dict.fromkeys(term.casefold() for term in re.findall(r"[^\W_]+", query))
        )
        if not terms:
            raise InvalidDocumentToolRequestError("query must contain a searchable word")
        matches: list[tuple[int, str, Node, str]] = []
        depth_by_node: dict[str, int] = {}
        section_by_key = {
            locator_key(section.locator): section.text for section in document.sections
        }
        search_by_key = {
            locator_key(section.locator): section.search_text
            for section in document.sections
        }
        for node in document.nodes:
            self._check_deadline(ctx.deadline_monotonic)
            depth_by_node[node.node_id] = (
                depth_by_node[node.parent_id] + 1
                if node.parent_id is not None
                else 0
            )
            section_text = section_by_key[locator_key(node.locator)]
            search_text = search_by_key[locator_key(node.locator)]
            title_terms = Counter(re.findall(r"[^\W_]+", node.title.casefold()))
            body_terms = Counter(
                re.findall(r"[^\W_]+", search_text.casefold())
            )
            score = sum(
                body_terms[term] + (2 * title_terms[term]) for term in terms
            )
            if score:
                matches.append((score, node.node_id, node, section_text))
        if not matches:
            raise NoDocumentMatchError("no indexed section matches the query")
        _, _, node, section_text = min(
            matches,
            key=lambda match: (
                -match[0],
                -depth_by_node[match[2].node_id],
                match[1],
            ),
        )
        value = self.dereference(source, node.locator)
        if value != section_text:
            raise DocumentSourceConflictError(
                "indexed section no longer matches the registered source"
            )
        return EvidenceRecord(
            source_id=source.source_id,
            source_version=source.version,
            node_id=node.node_id,
            locator=node.locator,
            operation=RETRIEVE_TOOL_ID,
            value=value,
            evidence_kind=EvidenceKind.OBSERVED,
            execution_id=str(uuid.uuid4()),
        )

    def _get_section(
        self, request: ToolRequest, ctx: RetrievalContext
    ) -> EvidenceRecord:
        parameters = request.parameters
        required = {"source_id", "source_version", "node_id"}
        if set(parameters) != required or any(
            not isinstance(parameters[key], str) or not parameters[key].strip()
            for key in required
        ):
            raise InvalidDocumentToolRequestError(
                "documents.get_section requires non-empty source_id, "
                "source_version and node_id"
            )
        if len(parameters["node_id"]) > MAX_NODE_ID_LENGTH:
            raise InvalidDocumentToolRequestError(
                f"node_id must be at most {MAX_NODE_ID_LENGTH} characters"
            )
        self._check_deadline(ctx.deadline_monotonic)
        source = self._source_for(
            parameters["source_id"], parameters["source_version"]
        )
        document = self._get_document(source)
        node = next(
            (item for item in document.nodes if item.node_id == parameters["node_id"]),
            None,
        )
        if node is None:
            raise DocumentSectionNotFoundError(
                "node_id is not a section of this document"
            )
        return EvidenceRecord(
            source_id=source.source_id,
            source_version=source.version,
            node_id=node.node_id,
            locator=node.locator,
            operation=SECTION_TOOL_ID,
            value=self.dereference(source, node.locator),
            evidence_kind=EvidenceKind.OBSERVED,
            execution_id=str(uuid.uuid4()),
        )

    def dereference(self, source: SourceRef, locator: NativeLocator) -> str:
        """Resolve a known locator against the exact registered source revision."""
        self._ensure_open()
        document = self._get_document(source)
        for section in document.sections:
            if section.locator == locator:
                return section.text
        raise KeyError("native document locator does not exist")

    def health(self) -> HealthResult:
        if self._closed:
            return HealthResult(HealthStatus.UNAVAILABLE, "document index is closed")
        return HealthResult(HealthStatus.HEALTHY)

    def close(self) -> None:
        self._documents.clear()
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise ClosedDocumentIndexError("document index is closed")

    @staticmethod
    def _check_deadline(deadline: float | None) -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise DocumentDeadlineExceededError("document operation deadline exceeded")

    def _source_for(self, source_id: str, version: str) -> SourceRef:
        document = self._documents.get((source_id, version))
        if document is None:
            raise DocumentNotRegisteredError(
                f"document source {source_id!r} version {version!r} is not registered"
            )
        return document.source

    def _get_document(self, source: SourceRef) -> IndexedDocument:
        document = self._documents.get((source.source_id, source.version))
        if document is None:
            raise DocumentNotRegisteredError(
                f"document source {source.source_id!r} version "
                f"{source.version!r} is not registered"
            )
        if document.source.source_type != source.source_type:
            raise DocumentSourceConflictError(
                "source type does not match the registered source revision"
            )
        return document


def load_plugin() -> DocumentIndexExtension:
    return DocumentIndexExtension()
