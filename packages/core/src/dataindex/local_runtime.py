"""The PageIndex-style local workflow behind ``DataIndexClient``.

``LocalRuntime`` lets one local user submit files, inspect them and chat over
them. It owns no policy of its own: every retrieval still goes through the
client and the central tool dispatcher, which check plugin trust, source access
and entitlement on each call. It only uses the client's public API and reaches
plugins through the small protocols in ``dataindex.local``, so core never
imports plugin packages.

The workspace stores each file's absolute path and the version that was indexed.
Before every use the file is checked again, so a document that changed after it
was submitted is reported as stale instead of being answered from old content.
"""

from __future__ import annotations

import os
import warnings
from collections import OrderedDict
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from dataindex._json import locator_json, plain_json
from dataindex.answers import ChatAnswer, abstained_answer, build_chat_answer
from dataindex.catalog import MAX_SOURCE_NODE_LIMIT, SQLiteCatalog
from dataindex.database_config import (
    database_version,
    normalize_database_config,
    query_summaries,
)
from dataindex.exceptions import (
    ConfigurationError,
    DataIndexError,
    DocumentNotFoundError,
    NodeNotFoundError,
    SourceAuthorizationError,
    SourceFileChangedError,
    SourceRejectedError,
    ToolNotAvailableError,
)
from dataindex.llm import (
    ChatModel,
    ModelPlanner,
    resolve_chat_model,
    validate_instructions,
)
from dataindex.local import (
    DATABASES_PLUGIN_ID,
    DOCUMENTS_PLUGIN_ID,
    MAX_SUBMITTED_FILE_BYTES,
    DatabaseRegistrar,
    DocumentRegistrar,
    LocalAuthorizer,
    classify_file,
    content_version,
    free_capabilities,
    install_hint,
    read_submitted_file,
    resolve_submitted_path,
    source_id_for,
)
from dataindex.models import (
    EvidenceRecord,
    ExtensionManifest,
    JsonValue,
    SourceRef,
    ToolRequest,
)
from dataindex.outline import (
    Outline,
    build_outline,
    limit_outline,
    outline_for_prompt,
    outline_tree,
)
from dataindex.reasoning import MAX_REASONING_SOURCES, PlannedToolCall, ReasoningBudget
from dataindex.workspace import (
    MAX_DOCUMENT_NAME_CHARS,
    MAX_WORKSPACE_DOCUMENTS,
    WorkspaceDocument,
    WorkspaceRegistry,
    validate_document_name,
)

if TYPE_CHECKING:
    from dataindex.sdk import DataIndexClient

DOCUMENT_SEARCH_TOOL = "documents.search"
DOCUMENT_SECTION_TOOL = "documents.get_section"
DATABASE_QUERY_TOOL = "databases.query"
DEFAULT_CHAT_BUDGET = ReasoningBudget(
    max_depth=3,
    max_tool_calls=8,
    max_tokens=100_000,
    timeout_seconds=120.0,
    maximum_results=100,
)
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 100
MAX_QUESTION_CHARS = 2_048
MAX_SEARCH_QUERY_CHARS = 256
MAX_NODE_ID_CHARS = 128
MAX_QUERY_ID_CHARS = 64
MAX_DATABASE_ROWS = 100
OUTLINE_CACHE_SIZE = 32
MAX_NAME_SUFFIX = 99
_MAX_NAME_EXTENSION_CHARS = 32
_PLUGIN_FOR_KIND = {"document": DOCUMENTS_PLUGIN_ID, "database": DATABASES_PLUGIN_ID}
_FILE_ERRORS = (OSError, RuntimeError, ValueError, ConfigurationError)

STATUS_COMPLETED = "completed"
STATUS_STALE = "stale"
STATUS_MISSING = "missing"
STATUS_UNAVAILABLE = "unavailable"
_MISSING_DETAIL = "file is missing; restore it or delete the document"


@dataclass(frozen=True, slots=True)
class _Inspection:
    """What is true of a submitted file right now."""

    status: str
    detail: str | None = None
    content: bytes | None = None


class LocalRuntime:
    """Submit, inspect and chat over files for one local ``DataIndexClient``.

    Not thread-safe: use one client per thread.
    """

    def __init__(
        self,
        client: DataIndexClient,
        catalog: SQLiteCatalog,
        workspace: WorkspaceRegistry,
        authorizer: LocalAuthorizer,
        model: ChatModel | None,
        instructions: str | None,
    ) -> None:
        self._client = client
        self._catalog = catalog
        self._workspace = workspace
        self._authorizer = authorizer
        self._model = model
        self._instructions = validate_instructions(instructions)
        self._ready: dict[str, SourceRef] = {}
        self._outlines: OrderedDict[tuple[str, str], Outline] = OrderedDict()
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._ready.clear()
        self._outlines.clear()
        self._workspace.close()

    # ------------------------------------------------------------------
    # Submitting and removing files

    def submit_document(
        self,
        path: str | os.PathLike[str],
        *,
        name: str | None = None,
        wait: bool = True,
    ) -> dict[str, JsonValue]:
        self._ensure_open()
        if not isinstance(wait, bool):
            raise ValueError("wait must be a bool")
        if name is not None:
            validate_document_name(name)
        resolved = resolve_submitted_path(path)
        kind = classify_file(resolved)
        if kind.kind != "document":
            raise ConfigurationError(
                f"{resolved.name!r} is a SQLite database; submit it with "
                "submit_database(path, allowed_columns=..., queries=...)"
            )
        content = read_submitted_file(resolved)
        doc_id = source_id_for(resolved, kind.kind)
        source = SourceRef(doc_id, kind.source_type, content_version(content))
        registrar, manifest = self._documents_plugin()
        existing = self._existing_for_submit(doc_id)
        display_name = self._unique_name(
            name
            if name is not None
            else (existing.name if existing is not None else resolved.name),
            doc_id,
        )
        self._ready.pop(doc_id, None)
        try:
            node_count = self._activate_document(
                registrar, manifest, source, content, resolved.name
            )
            self._workspace.upsert(
                WorkspaceDocument(
                    doc_id=doc_id,
                    name=display_name,
                    kind=kind.kind,
                    source_type=kind.source_type,
                    plugin_id=kind.plugin_id,
                    path=str(resolved),
                    version=source.version,
                )
            )
        except BaseException:
            if existing is None:
                self._discard(doc_id, kind.kind)
            raise
        self._ready[doc_id] = source
        return self._submitted(doc_id, display_name, source, existing, node_count)

    def submit_database(
        self,
        path: str | os.PathLike[str],
        *,
        allowed_columns: Mapping[str, Sequence[str]],
        queries: Sequence[object],
        name: str | None = None,
    ) -> dict[str, JsonValue]:
        self._ensure_open()
        if name is not None:
            validate_document_name(name)
        resolved = resolve_submitted_path(path)
        kind = classify_file(resolved)
        if kind.kind != "database":
            raise ConfigurationError(
                f"{resolved.name!r} is a document; submit it with "
                "submit_document(path)"
            )
        config = normalize_database_config(allowed_columns, queries)
        registrar, manifest = self._databases_plugin()
        doc_id = source_id_for(resolved, kind.kind)
        existing = self._existing_for_submit(doc_id)
        with _rejecting(resolved.name):
            file_state = registrar.database_source_version(resolved)
        source = SourceRef(
            doc_id, kind.source_type, database_version(file_state, config)
        )
        display_name = self._unique_name(
            name
            if name is not None
            else (existing.name if existing is not None else resolved.name),
            doc_id,
        )
        self._ready.pop(doc_id, None)
        try:
            node_count = self._activate_database(
                registrar, manifest, source, resolved, config, resolved.name
            )
            self._workspace.upsert(
                WorkspaceDocument(
                    doc_id=doc_id,
                    name=display_name,
                    kind=kind.kind,
                    source_type=kind.source_type,
                    plugin_id=kind.plugin_id,
                    path=str(resolved),
                    version=source.version,
                    config=config,
                )
            )
        except BaseException:
            if existing is None:
                self._discard(doc_id, kind.kind)
            raise
        self._ready[doc_id] = source
        return self._submitted(doc_id, display_name, source, existing, node_count)

    def delete_document(self, doc_id: str) -> bool:
        self._ensure_open()
        _require_doc_id(doc_id)
        row = self._workspace.get(doc_id)
        if row is None:
            return False
        self._discard(doc_id, row.kind)
        self._workspace.delete(doc_id)
        return True

    # ------------------------------------------------------------------
    # Looking at submitted files

    def get_document(self, doc_id: str) -> dict[str, JsonValue]:
        self._ensure_open()
        row = self._lookup(doc_id)
        return self._describe(row, self._inspect(row, {}))

    def list_documents(
        self, limit: int = DEFAULT_LIST_LIMIT, offset: int = 0
    ) -> dict[str, JsonValue]:
        self._ensure_open()
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_LIST_LIMIT
        ):
            raise ValueError(f"limit must be between 1 and {MAX_LIST_LIMIT}")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        rows = self._workspace.list_documents(
            limit=limit, offset=offset, newest_first=True
        )
        availability: dict[str, str | None] = {}
        documents: list[JsonValue] = [
            self._describe(row, self._inspect(row, availability)) for row in rows
        ]
        return {
            "documents": documents,
            "total": self._workspace.count(),
            "limit": limit,
            "offset": offset,
        }

    def get_document_id(self, name: str) -> str:
        self._ensure_open()
        validate_document_name(name)
        row = self._workspace.get_by_name(name)
        if row is None:
            raise DocumentNotFoundError(f"no document is named {name!r}")
        return row.doc_id

    def get_tree(
        self, doc_id: str, *, max_entries: int | None = None
    ) -> dict[str, JsonValue]:
        self._ensure_open()
        if max_entries is not None and (
            isinstance(max_entries, bool)
            or not isinstance(max_entries, int)
            or max_entries < 1
        ):
            raise ValueError("max_entries must be a positive integer")
        row, source = self._ready_document(doc_id)
        if self._client.get_source(source.source_id) != source:
            raise SourceAuthorizationError(
                f"tenant is not authorized for {row.name!r}"
            )
        outline = self._outline(source)
        if max_entries is not None:
            outline = limit_outline(outline, max_entries)
        return {
            "doc_id": row.doc_id,
            "name": row.name,
            "status": STATUS_COMPLETED,
            "truncated": outline.truncated,
            "result": outline_tree(outline),
        }

    def get_section(self, doc_id: str, node_id: str) -> dict[str, JsonValue]:
        self._ensure_open()
        if (
            not isinstance(node_id, str)
            or not 1 <= len(node_id) <= MAX_NODE_ID_CHARS
        ):
            raise ValueError(
                f"node_id must be a string of 1 to {MAX_NODE_ID_CHARS} characters"
            )
        row, source = self._ready_document(doc_id, needs="get_section")
        node = self._catalog.get_node(node_id)
        if (
            node is None
            or node.source_id != source.source_id
            or node.source_version != source.version
        ):
            raise NodeNotFoundError(
                f"{row.name!r} has no section {node_id[:64]!r}; use get_tree to "
                "list its sections"
            )
        evidence = self._retrieve(
            DOCUMENTS_PLUGIN_ID,
            source,
            DOCUMENT_SECTION_TOOL,
            {"node_id": node_id},
        )
        return {
            "doc_id": row.doc_id,
            "name": row.name,
            "node_id": node.node_id,
            "title": node.title,
            "locator": locator_json(evidence.locator),
            "text": plain_json(evidence.value),
            "source_version": evidence.source_version,
            "operation": evidence.operation,
            "evidence_id": evidence.execution_id,
        }

    # ------------------------------------------------------------------
    # Retrieval without a chat model (used by the agent tools)

    def search_document(self, doc_id: str, query: str) -> dict[str, JsonValue]:
        self._ensure_open()
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        text = query.strip()
        if len(text) > MAX_SEARCH_QUERY_CHARS:
            raise ValueError(
                f"query must be at most {MAX_SEARCH_QUERY_CHARS} characters"
            )
        row, source = self._ready_document(doc_id, needs="search_document")
        evidence = self._retrieve(
            DOCUMENTS_PLUGIN_ID, source, DOCUMENT_SEARCH_TOOL, {"query": text}
        )
        return _evidence_view(row, evidence)

    def query_database(
        self,
        doc_id: str,
        query_id: str,
        filters: Mapping[str, object] | None = None,
        limit: int | None = None,
    ) -> dict[str, JsonValue]:
        self._ensure_open()
        if (
            not isinstance(query_id, str)
            or not 1 <= len(query_id) <= MAX_QUERY_ID_CHARS
        ):
            raise ValueError(
                f"query_id must be a string of 1 to {MAX_QUERY_ID_CHARS} characters"
            )
        if filters is not None and not isinstance(filters, Mapping):
            raise ValueError("filters must be an object")
        if limit is not None and (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_DATABASE_ROWS
        ):
            raise ValueError(f"limit must be between 1 and {MAX_DATABASE_ROWS}")
        row = self._lookup(doc_id)
        if row.kind != "database":
            raise ConfigurationError(
                f"{row.name!r} is a document; use search_document or get_section"
            )
        source = self._prepare(row)
        parameters: dict[str, JsonValue] = {
            "query_id": query_id,
            "filters": plain_json(dict(filters or {})),
        }
        if limit is not None:
            parameters["limit"] = limit
        evidence = self._retrieve(
            DATABASES_PLUGIN_ID, source, DATABASE_QUERY_TOOL, parameters
        )
        return _evidence_view(row, evidence)

    # ------------------------------------------------------------------
    # Chat

    def chat(
        self,
        question: str,
        doc_id: str | Sequence[str] | None = None,
        *,
        budget: ReasoningBudget | None = None,
        instructions: str | None = None,
    ) -> ChatAnswer:
        self._ensure_open()
        if (
            not isinstance(question, str)
            or not question.strip()
            or len(question) > MAX_QUESTION_CHARS
        ):
            raise ValueError(
                f"question must be a string of 1 to {MAX_QUESTION_CHARS} characters"
            )
        if budget is None:
            budget = DEFAULT_CHAT_BUDGET
        elif not isinstance(budget, ReasoningBudget):
            raise ValueError("budget must be a ReasoningBudget")
        effective = (
            self._instructions
            if instructions is None
            else validate_instructions(instructions)
        )
        model = resolve_chat_model(self._model)
        explicit = doc_id is not None
        if doc_id is None:
            targets = [row.doc_id for row in self._workspace.list_documents()]
            if len(targets) > MAX_REASONING_SOURCES:
                raise ConfigurationError(
                    f"the workspace has {len(targets)} documents but one question "
                    f"can read at most {MAX_REASONING_SOURCES}; pass doc_id=[...] "
                    "to choose which ones"
                )
        else:
            targets = _document_ids(doc_id)
        sources: list[SourceRef] = []
        names: dict[str, str] = {}
        skipped: list[str] = []
        for target in targets:
            row = self._lookup(target) if explicit else self._workspace.get(target)
            if row is None:
                continue
            try:
                source = self._prepare(row)
            except (
                ConfigurationError,
                SourceFileChangedError,
                SourceRejectedError,
            ) as error:
                if explicit:
                    raise
                skipped.append(f"skipped {error}")
                continue
            sources.append(source)
            names[source.source_id] = row.name
        if not sources:
            reason = (
                "No documents have been submitted yet; submit a document first."
                if not targets
                else "None of the submitted documents could be read."
            )
            return abstained_answer(reason, warnings=tuple(skipped))
        planner = ModelPlanner(
            model,
            source_context=lambda source, entries: self._source_context(
                source, names, entries
            ),
            max_depth=budget.max_depth,
            instructions=effective,
        )
        result = self._client.ask(
            question,
            sources,
            planner,
            budget=budget,
            require_source_match=False,
            tolerate_tool_failures=True,
        )
        return build_chat_answer(result, names=names, warnings=tuple(skipped))

    # ------------------------------------------------------------------
    # Readiness: re-check the file, then re-activate it after a restart

    def _lookup(self, doc_id: str) -> WorkspaceDocument:
        _require_doc_id(doc_id)
        row = self._workspace.get(doc_id)
        if row is None:
            raise DocumentNotFoundError(f"no document has the id {doc_id[:64]!r}")
        return row

    def _ready_document(
        self, doc_id: str, *, needs: str | None = None
    ) -> tuple[WorkspaceDocument, SourceRef]:
        row = self._lookup(doc_id)
        if needs is not None and row.kind != "document":
            raise ConfigurationError(
                f"{row.name!r} is a database; {needs} reads documents. Use "
                "query_database for databases"
            )
        return row, self._prepare(row)

    def _prepare(self, row: WorkspaceDocument) -> SourceRef:
        """Return the source of ``row`` once its file is verified and registered."""
        state = self._inspect(row, {})
        if state.status in (STATUS_MISSING, STATUS_STALE):
            raise SourceFileChangedError(f"{row.name!r}: {state.detail}")
        if state.status == STATUS_UNAVAILABLE:
            raise ConfigurationError(f"{row.name!r}: {state.detail}")
        source = SourceRef(row.doc_id, row.source_type, row.version)
        if self._ready.get(row.doc_id) != source:
            if row.kind == "document" and state.content is not None:
                registrar, manifest = self._documents_plugin()
                self._activate_document(
                    registrar, manifest, source, state.content, row.name
                )
            elif row.kind == "database" and isinstance(row.config, Mapping):
                database, manifest = self._databases_plugin()
                self._activate_database(
                    database, manifest, source, Path(row.path), row.config, row.name
                )
            else:
                raise SourceFileChangedError(
                    f"{row.name!r}: its stored state is incomplete; submit it again"
                )
            self._ready[row.doc_id] = source
        return source

    def _inspect(
        self, row: WorkspaceDocument, availability: dict[str, str | None]
    ) -> _Inspection:
        """Compare the file on disk with what was submitted, then check its plugin.

        The file is checked first so a missing or changed file is reported even
        when its plugin is also unavailable.
        """
        plugin_id = _PLUGIN_FOR_KIND[row.kind]
        content: bytes | None = None
        if row.kind == "document":
            try:
                content = read_submitted_file(resolve_submitted_path(row.path))
            except FileNotFoundError:
                return _Inspection(STATUS_MISSING, _MISSING_DETAIL)
            except _FILE_ERRORS:
                return _Inspection(
                    STATUS_STALE,
                    "file could not be read or is larger than "
                    f"{MAX_SUBMITTED_FILE_BYTES // (1024 * 1024)} MiB; fix it and "
                    "submit it again",
                )
            if content_version(content) != row.version:
                return _Inspection(
                    STATUS_STALE,
                    "file changed after it was submitted; submit it again",
                )
        else:
            try:
                present = Path(row.path).is_file()
            except (OSError, ValueError):
                present = False
            if not present:
                return _Inspection(STATUS_MISSING, _MISSING_DETAIL)
        if plugin_id not in availability:
            availability[plugin_id] = self._availability(plugin_id)
        problem = availability[plugin_id]
        if problem is not None:
            return _Inspection(STATUS_UNAVAILABLE, problem)
        if row.kind == "database":
            return self._inspect_database(row)
        return _Inspection(STATUS_COMPLETED, content=content)

    def _inspect_database(self, row: WorkspaceDocument) -> _Inspection:
        if not isinstance(row.config, Mapping):
            return _Inspection(
                STATUS_STALE,
                "approved-query configuration is missing; submit it again",
            )
        registrar, _ = self._databases_plugin()
        try:
            version = database_version(
                registrar.database_source_version(Path(row.path)), row.config
            )
        except Exception:
            return _Inspection(
                STATUS_STALE,
                "file could not be opened as a SQLite database; fix it and "
                "submit it again",
            )
        if version != row.version:
            return _Inspection(
                STATUS_STALE,
                "database file or approved queries changed after it was "
                "submitted; submit it again",
            )
        return _Inspection(STATUS_COMPLETED)

    # ------------------------------------------------------------------
    # Registration with the plugins, the catalog and the authorizer

    def _activate_document(
        self,
        registrar: DocumentRegistrar,
        manifest: ExtensionManifest,
        source: SourceRef,
        content: bytes,
        label: str,
    ) -> int:
        with _rejecting(label):
            registrar.register_document(source, content)
        self._authorizer.grant(source, free_capabilities(manifest))
        self._client.register_source(source)
        return self._index(source, DOCUMENTS_PLUGIN_ID, label)

    def _activate_database(
        self,
        registrar: DatabaseRegistrar,
        manifest: ExtensionManifest,
        source: SourceRef,
        path: Path,
        config: Mapping[str, JsonValue],
        label: str,
    ) -> int:
        with _rejecting(label):
            registrar.unregister_database(source.source_id)
            registrar.register_database_config(source, path, config)
        try:
            current: str | None = database_version(
                registrar.database_source_version(path), config
            )
        except Exception:
            current = None
        if current != source.version:
            with suppress(Exception):
                registrar.unregister_database(source.source_id)
            raise SourceFileChangedError(
                f"{label!r} changed while it was being registered; try again"
            )
        self._authorizer.grant(source, free_capabilities(manifest))
        self._client.register_source(source)
        return self._index(source, DATABASES_PLUGIN_ID, label)

    def _index(self, source: SourceRef, plugin_id: str, label: str) -> int:
        """Index ``source`` unless its current revision already has nodes."""
        indexed = self._catalog.list_source_nodes(
            source.source_id, limit=MAX_SOURCE_NODE_LIMIT
        )
        if indexed:
            return len(indexed)
        try:
            with _rejecting(label):
                return len(self._client.index(source, plugin_id))
        except ToolNotAvailableError as error:
            raise SourceRejectedError(
                f"{label!r} was rejected: {error}", error_type=type(error).__name__
            ) from error

    def _discard(self, doc_id: str, kind: str) -> None:
        """Best-effort removal of everything registered for a file."""
        self._authorizer.revoke(doc_id)
        with suppress(DataIndexError):
            self._catalog.remove_source(doc_id)
        # Cleanup must not hide the error that triggered it.
        with suppress(Exception):
            extension = self._client.extension(_PLUGIN_FOR_KIND[kind])
            unregister = getattr(
                extension,
                "unregister_document" if kind == "document" else "unregister_database",
                None,
            )
            if callable(unregister):
                unregister(doc_id)
        self._ready.pop(doc_id, None)
        for key in [key for key in self._outlines if key[0] == doc_id]:
            del self._outlines[key]

    # ------------------------------------------------------------------
    # Plugins

    def _documents_plugin(self) -> tuple[DocumentRegistrar, ExtensionManifest]:
        extension = self._extension(DOCUMENTS_PLUGIN_ID)
        if not isinstance(extension, DocumentRegistrar):
            raise ConfigurationError(_too_old(DOCUMENTS_PLUGIN_ID))
        return extension, extension.manifest()

    def _databases_plugin(self) -> tuple[DatabaseRegistrar, ExtensionManifest]:
        extension = self._extension(DATABASES_PLUGIN_ID)
        if not isinstance(extension, DatabaseRegistrar):
            raise ConfigurationError(_too_old(DATABASES_PLUGIN_ID))
        return extension, extension.manifest()

    def _extension(self, plugin_id: str) -> object:
        try:
            return self._client.extension(plugin_id)
        except ToolNotAvailableError as error:
            raise ConfigurationError(self._plugin_problem(plugin_id)) from error

    def _availability(self, plugin_id: str) -> str | None:
        try:
            if plugin_id == DOCUMENTS_PLUGIN_ID:
                self._documents_plugin()
            else:
                self._databases_plugin()
        except ConfigurationError as error:
            return str(error)
        return None

    def _plugin_problem(self, plugin_id: str) -> str:
        """Explain why a plugin cannot be used, from status flags only.

        The text is built from booleans and fixed hints, never from anything the
        plugin supplied.
        """
        hint = install_hint(plugin_id)
        status = next(
            (
                item
                for item in self._client.list_extensions()
                if item.plugin_id == plugin_id
            ),
            None,
        )
        if status is None:
            return f"the {plugin_id!r} plugin is not installed; run: {hint}"
        if not status.compatible:
            return (
                f"the installed {plugin_id!r} plugin is not compatible with this "
                f"DataIndex version; upgrade it: {hint} --upgrade"
            )
        if not status.trusted:
            return (
                f"the {plugin_id!r} plugin is installed but not trusted; add its "
                "distribution to trusted_plugins"
            )
        if not status.enabled:
            return f"the {plugin_id!r} plugin is not enabled"
        return (
            f"the {plugin_id!r} plugin is not healthy; run `dataindex extensions "
            "list` for details"
        )

    # ------------------------------------------------------------------
    # Small helpers

    def _existing_for_submit(self, doc_id: str) -> WorkspaceDocument | None:
        existing = self._workspace.get(doc_id)
        if existing is None and self._workspace.count() >= MAX_WORKSPACE_DOCUMENTS:
            raise ConfigurationError(
                f"the workspace already holds {MAX_WORKSPACE_DOCUMENTS} documents; "
                "delete some before submitting more"
            )
        return existing

    def _unique_name(self, desired: str, doc_id: str) -> str:
        """Return ``desired``, or ``desired`` with ``_N`` before its extension."""
        validate_document_name(desired)
        holder = self._workspace.get_by_name(desired)
        if holder is None or holder.doc_id == doc_id:
            return desired
        stem, extension = os.path.splitext(desired)
        if len(extension) > _MAX_NAME_EXTENSION_CHARS:
            stem, extension = desired, ""
        for number in range(1, MAX_NAME_SUFFIX + 1):
            suffix = f"_{number}"
            room = MAX_DOCUMENT_NAME_CHARS - len(extension) - len(suffix)
            candidate = f"{stem[:room]}{suffix}{extension}"
            holder = self._workspace.get_by_name(candidate)
            if holder is None or holder.doc_id == doc_id:
                warnings.warn(
                    f"a document named {desired!r} already exists; this one is "
                    f"named {candidate!r}",
                    UserWarning,
                    stacklevel=4,
                )
                return candidate
        raise ConfigurationError(
            f"{MAX_NAME_SUFFIX} documents already use names like {desired!r}; "
            "pass a different name"
        )

    def _submitted(
        self,
        doc_id: str,
        name: str,
        source: SourceRef,
        existing: WorkspaceDocument | None,
        node_count: int,
    ) -> dict[str, JsonValue]:
        if existing is None:
            change = "new"
        elif existing.version != source.version:
            change = "updated"
        else:
            change = "unchanged"
        return {
            "doc_id": doc_id,
            "name": name,
            "status": STATUS_COMPLETED,
            "change": change,
            "source_type": source.source_type,
            "version": source.version,
            "node_count": node_count,
        }

    def _describe(
        self, row: WorkspaceDocument, state: _Inspection
    ) -> dict[str, JsonValue]:
        info: dict[str, JsonValue] = {
            "id": row.doc_id,
            "doc_id": row.doc_id,
            "name": row.name,
            "description": None,
            "status": state.status,
            "createdAt": row.submitted_at,
            "kind": row.kind,
            "source_type": row.source_type,
            "plugin_id": _PLUGIN_FOR_KIND[row.kind],
            "version": row.version,
            "path": row.path,
        }
        if state.status != STATUS_COMPLETED and state.detail is not None:
            info["detail"] = state.detail
        if row.kind == "database":
            info["queries"] = query_summaries(row.config)
        return info

    def _outline(self, source: SourceRef) -> Outline:
        key = (source.source_id, source.version)
        cached = self._outlines.get(key)
        if cached is not None:
            self._outlines.move_to_end(key)
            return cached
        outline = build_outline(self._catalog, source)
        self._outlines[key] = outline
        while len(self._outlines) > OUTLINE_CACHE_SIZE:
            self._outlines.popitem(last=False)
        return outline

    def _source_context(
        self, source: SourceRef, names: Mapping[str, str], max_entries: int
    ) -> dict[str, JsonValue]:
        details: dict[str, JsonValue] = {"name": names.get(source.source_id, "")}
        details.update(outline_for_prompt(self._outline(source), max_entries))
        return details

    def _retrieve(
        self,
        plugin_id: str,
        source: SourceRef,
        tool_id: str,
        parameters: Mapping[str, JsonValue],
    ) -> EvidenceRecord:
        request = ToolRequest(
            tool_id,
            {
                "source_id": source.source_id,
                "source_version": source.version,
                **parameters,
            },
        )
        return self._client.retrieve(PlannedToolCall(plugin_id, source, request))

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("DataIndexClient is closed")


@contextmanager
def _rejecting(label: str) -> Generator[None, None, None]:
    """Report a plugin failure as a rejected file.

    DataIndex errors and resource exhaustion pass through unchanged. The message
    is for the local user only and is never sent to a model.
    """
    try:
        yield
    except (DataIndexError, MemoryError, RecursionError):
        raise
    except Exception as error:
        raise SourceRejectedError(
            f"{label!r} was rejected: {error}", error_type=type(error).__name__
        ) from error


def _too_old(plugin_id: str) -> str:
    return (
        f"the installed {plugin_id!r} plugin is too old for this DataIndex "
        f"version; upgrade it: {install_hint(plugin_id)} --upgrade"
    )


def _require_doc_id(doc_id: object) -> None:
    if not isinstance(doc_id, str) or not doc_id:
        raise ValueError("doc_id must be a non-empty string")


def _document_ids(doc_id: str | Sequence[str]) -> list[str]:
    if isinstance(doc_id, str):
        requested: list[object] = [doc_id]
    elif isinstance(doc_id, Sequence) and not isinstance(doc_id, (bytes, bytearray)):
        requested = list(doc_id)
    else:
        raise ValueError("doc_id must be a document ID, a list of IDs or None")
    ids: list[str] = []
    for item in requested:
        _require_doc_id(item)
        assert isinstance(item, str)
        if item not in ids:
            ids.append(item)
    if not 1 <= len(ids) <= MAX_REASONING_SOURCES:
        raise ValueError(
            f"doc_id must name between 1 and {MAX_REASONING_SOURCES} documents"
        )
    return ids


def _evidence_view(
    row: WorkspaceDocument, evidence: EvidenceRecord
) -> dict[str, JsonValue]:
    view: dict[str, JsonValue] = {
        "doc_id": row.doc_id,
        "name": row.name,
        "source_version": evidence.source_version,
        "operation": evidence.operation,
        "evidence_id": evidence.execution_id,
        "evidence_kind": evidence.evidence_kind.value,
        "locator": locator_json(evidence.locator),
        "node_id": evidence.node_id,
        "value": plain_json(evidence.value),
    }
    if evidence.unit is not None:
        view["unit"] = evidence.unit
    if evidence.warnings:
        view["warnings"] = list(evidence.warnings)
    return view


__all__ = ["DEFAULT_CHAT_BUDGET", "LocalRuntime"]
