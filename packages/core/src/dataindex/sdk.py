"""The DataIndex client: a PageIndex-style local workflow and a host-facing core.

``DataIndexClient(chat=...)`` is the local client. It keeps a workspace on disk,
trusts only the installed first-party free plugins and authorizes only the files
its user submitted. Hosts that bring their own tenant authorizer, entitlement
provider and plugin allowlist use ``DataIndexClient.open`` instead, and then
drive indexing and retrieval through the lower-level methods below.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import ExitStack, suppress
from pathlib import Path

from dataindex.agent_tools import (
    AgentTool,
    agent_instructions,
    agent_tool_definitions,
    call_agent_tool,
)
from dataindex.answers import ChatAnswer
from dataindex.catalog import SQLiteCatalog
from dataindex.entitlements import EntitlementProvider
from dataindex.exceptions import (
    ChatModelConfigurationError,
    ConfigurationError,
    SourceAuthorizationError,
    StaleSourceVersionError,
    ToolNotAvailableError,
)
from dataindex.llm import ChatModel, resolve_chat_model, validate_instructions
from dataindex.local import (
    CATALOG_FILE_NAME,
    DEFAULT_STORAGE_PATH,
    LOCAL_DEPLOYMENT,
    LocalAuthorizer,
    load_local_registry,
    resolve_trusted_plugins,
)
from dataindex.local_runtime import LocalRuntime
from dataindex.models import (
    DataIndexExtension,
    EvidenceRecord,
    HealthStatus,
    IndexContext,
    JsonValue,
    Node,
    SourceRef,
)
from dataindex.reasoning import (
    LLMPlanner,
    PlannedToolCall,
    ReasoningBudget,
    ReasoningEngine,
    ReasoningResult,
    SourceAuthorizer,
    ToolDispatcher,
)
from dataindex.registry import ExtensionRegistry, PluginStatus
from dataindex.workspace import WORKSPACE_FILE_NAME, WorkspaceRegistry

MAX_INDEX_RECORDS = 10_000
MAX_INDEX_TIMEOUT_SECONDS = 300.0


class DataIndexClient:
    """Orchestrate the core, either as a local client or under host policies.

    ``DataIndexClient(chat="model-name")`` creates a local client. Files are
    submitted by path, indexed by the installed first-party plugin, and
    answered through the central dispatcher, which still performs source
    authorization and license entitlement checks before every tool call. Use
    one client per thread, and keep ``tenant_id`` the same for a given
    ``storage_path``: sources are owned by the tenant that registered them.

    ``chat`` is a model name, which needs the optional ``dataindex-llm`` package,
    or an object with a ``complete()`` method. ``chat_backend`` may carry
    ``api_key`` and ``api_base`` for a named model. ``trusted_plugins`` replaces
    the default of trusting the installed first-party free plugins; premium and
    third-party plugins are never trusted unless listed there.

    ``DataIndexClient.open`` is the host entry point with an explicit authorizer,
    allowlist and entitlement provider; such a client has no local workflow.
    """

    def __init__(
        self,
        chat: ChatModel | str | None = None,
        *,
        chat_backend: Mapping[str, str] | None = None,
        storage_path: str | os.PathLike[str] = DEFAULT_STORAGE_PATH,
        tenant_id: str = "local",
        trusted_plugins: Iterable[str | tuple[str, str]] | None = None,
        instructions: str | None = None,
    ) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")
        directory = _storage_directory(storage_path)
        default_instructions = validate_instructions(instructions)
        trusted = resolve_trusted_plugins(trusted_plugins)
        if chat is None:
            if chat_backend is not None:
                raise ChatModelConfigurationError(
                    "chat_backend needs chat to name a model"
                )
            model = None
        else:
            model = resolve_chat_model(chat, chat_backend)
        directory.mkdir(parents=True, exist_ok=True)
        catalog = SQLiteCatalog(directory / CATALOG_FILE_NAME)
        workspace: WorkspaceRegistry | None = None
        registry: ExtensionRegistry | None = None
        try:
            workspace = WorkspaceRegistry(directory / WORKSPACE_FILE_NAME)
            registry = load_local_registry(trusted)
            authorizer = LocalAuthorizer(tenant_id)
            self._attach(
                catalog,
                registry,
                tenant_id=tenant_id,
                source_authorizer=authorizer,
                entitlement_provider=None,
                deployment=LOCAL_DEPLOYMENT,
            )
            self._runtime = LocalRuntime(
                self, catalog, workspace, authorizer, model, default_instructions
            )
        except BaseException:
            closers = [catalog.close]
            if workspace is not None:
                closers.append(workspace.close)
            if registry is not None:
                closers.extend(
                    extension.close for extension in registry.loaded_extensions.values()
                )
            _close_quietly(closers)
            raise

    @classmethod
    def from_components(
        cls,
        catalog: SQLiteCatalog,
        registry: ExtensionRegistry,
        *,
        tenant_id: str,
        source_authorizer: SourceAuthorizer,
        entitlement_provider: EntitlementProvider | None,
        deployment: str,
    ) -> DataIndexClient:
        """Wrap an existing catalog and registry; the client then owns them."""
        client = cls.__new__(cls)
        client._attach(
            catalog,
            registry,
            tenant_id=tenant_id,
            source_authorizer=source_authorizer,
            entitlement_provider=entitlement_provider,
            deployment=deployment,
        )
        return client

    def _attach(
        self,
        catalog: SQLiteCatalog,
        registry: ExtensionRegistry,
        *,
        tenant_id: str,
        source_authorizer: SourceAuthorizer,
        entitlement_provider: EntitlementProvider | None,
        deployment: str,
    ) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")
        self._catalog = catalog
        self._registry = registry
        self.tenant_id = tenant_id
        self._source_authorizer = source_authorizer
        self._closed = False
        self._runtime: LocalRuntime | None = None
        self._dispatcher = ToolDispatcher(
            registry,
            catalog,
            source_authorizer,
            entitlement_provider,
            deployment=deployment,
        )
        self._engine = ReasoningEngine(self._dispatcher, catalog)

    @classmethod
    def open(
        cls,
        catalog_path: str | Path = ":memory:",
        *,
        tenant_id: str,
        source_authorizer: SourceAuthorizer,
        trusted_distributions: Iterable[tuple[str, str]] = (),
        enabled_plugin_ids: Iterable[str] = (),
        entitlement_provider: EntitlementProvider | None = None,
        deployment: str,
    ) -> DataIndexClient:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")
        if not isinstance(deployment, str) or not deployment.strip():
            raise ValueError("deployment must be a non-empty string")
        registry = ExtensionRegistry(
            trusted_distributions=trusted_distributions,
            enabled_plugin_ids=enabled_plugin_ids,
        )
        registry.discover()
        catalog = SQLiteCatalog(catalog_path)
        registry.load_enabled()
        return cls.from_components(
            catalog,
            registry,
            tenant_id=tenant_id,
            source_authorizer=source_authorizer,
            entitlement_provider=entitlement_provider,
            deployment=deployment,
        )

    def register_source(self, source: SourceRef) -> None:
        self._ensure_open()
        if not self._source_authorizer.is_authorized(
            self.tenant_id, source, "source.access"
        ):
            raise SourceAuthorizationError(
                f"tenant is not authorized for source {source.source_id!r}"
            )
        owner = self._catalog.get_source_tenant(source.source_id)
        if owner is None:
            self._catalog.create_source(source, tenant_id=self.tenant_id)
            return
        if owner != self.tenant_id:
            raise SourceAuthorizationError(
                f"source {source.source_id!r} belongs to another tenant"
            )
        current = self._catalog.get_source(source.source_id)
        if current != source:
            self._catalog.update_source(source)

    def index(
        self,
        source: SourceRef,
        plugin_id: str,
        *,
        timeout_seconds: float = 30.0,
        maximum_records: int = MAX_INDEX_RECORDS,
    ) -> tuple[Node, ...]:
        self._ensure_open()
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= MAX_INDEX_TIMEOUT_SECONDS
        ):
            raise ValueError(
                f"timeout_seconds must be greater than 0 and at most "
                f"{MAX_INDEX_TIMEOUT_SECONDS}"
            )
        if (
            isinstance(maximum_records, bool)
            or not isinstance(maximum_records, int)
            or not 1 <= maximum_records <= MAX_INDEX_RECORDS
        ):
            raise ValueError(
                f"maximum_records must be between 1 and {MAX_INDEX_RECORDS}"
            )
        self._authorize_source(source)
        extension = self.extension(plugin_id)
        manifest = extension.manifest()
        index_capabilities = tuple(
            capability
            for capability in manifest.capabilities
            if capability.endswith(".index")
        )
        if len(index_capabilities) != 1:
            raise ToolNotAvailableError(
                f"plugin {plugin_id!r} must declare exactly one *.index capability"
            )
        index_capability = index_capabilities[0]
        if not self._source_authorizer.is_authorized(
            self.tenant_id, source, index_capability
        ):
            raise SourceAuthorizationError(
                f"tenant is not authorized for capability {index_capability!r}"
            )
        if source.source_type not in manifest.source_types:
            raise ToolNotAvailableError(
                f"plugin {plugin_id!r} does not support {source.source_type!r}"
            )
        probe = extension.probe(source)
        if not probe.supported:
            raise ToolNotAvailableError(
                probe.reason or f"plugin {plugin_id!r} does not support this source"
            )
        deadline = time.monotonic() + timeout_seconds
        records: list[Node] = []
        seen_ids: set[str] = set()
        for node in extension.iter_index_records(
            source, IndexContext(deadline_monotonic=deadline)
        ):
            if time.monotonic() > deadline:
                raise TimeoutError("extension indexing exceeded its deadline")
            if not isinstance(node, Node):
                raise TypeError("extensions must yield Node records")
            if node.source_id != source.source_id or node.source_version != source.version:
                raise StaleSourceVersionError(
                    "extension index record does not match the registered source"
                )
            if node.node_id in seen_ids:
                raise ValueError(f"extension yielded duplicate node {node.node_id!r}")
            seen_ids.add(node.node_id)
            records.append(node)
            if len(records) > maximum_records:
                raise ValueError(
                    f"extension exceeded the {maximum_records}-record index limit"
                )
        if time.monotonic() > deadline:
            raise TimeoutError("extension indexing exceeded its deadline")
        self._catalog.upsert_nodes(records)
        return tuple(records)

    def search(
        self, source: SourceRef, query: str, *, limit: int = 100
    ) -> tuple[Node, ...]:
        self._ensure_open()
        self._authorize_source(source)
        return self._catalog.search_nodes(
            query, source_id=source.source_id, limit=limit
        )

    def get_source(self, source_id: str) -> SourceRef | None:
        self._ensure_open()
        if self._catalog.get_source_tenant(source_id) != self.tenant_id:
            return None
        source = self._catalog.get_source(source_id)
        if source is None or not self._source_authorizer.is_authorized(
            self.tenant_id, source, "source.access"
        ):
            return None
        return source

    def list_sources(self, *, limit: int = 100) -> tuple[SourceRef, ...]:
        self._ensure_open()
        return tuple(
            source
            for source in self._catalog.list_sources(self.tenant_id, limit=limit)
            if self._source_authorizer.is_authorized(
                self.tenant_id, source, "source.access"
            )
        )

    def retrieve(
        self,
        call: PlannedToolCall,
        *,
        timeout_seconds: float = 30.0,
        maximum_results: int = 100,
    ) -> EvidenceRecord:
        self._ensure_open()
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 300.0
        ):
            raise ValueError("timeout_seconds must be greater than 0 and at most 300")
        return self._dispatcher.dispatch(
            self.tenant_id,
            call,
            deadline_monotonic=time.monotonic() + timeout_seconds,
            maximum_results=maximum_results,
        )

    def ask(
        self,
        question: str,
        sources: Sequence[SourceRef],
        planner: LLMPlanner,
        *,
        budget: ReasoningBudget = ReasoningBudget(),
        require_source_match: bool = True,
        tolerate_tool_failures: bool = False,
    ) -> ReasoningResult:
        self._ensure_open()
        return self._engine.run_with_planner(
            tenant_id=self.tenant_id,
            question=question,
            sources=sources,
            planner=planner,
            budget=budget,
            require_source_match=require_source_match,
            tolerate_tool_failures=tolerate_tool_failures,
        )

    # ------------------------------------------------------------------
    # Local workflow: submit files, then chat over them

    def submit_document(
        self,
        path: str | os.PathLike[str],
        *,
        name: str | None = None,
        wait: bool = True,
    ) -> dict[str, JsonValue]:
        """Index a Markdown, text or HTML file; return its ``doc_id`` and status.

        Submitting the same path again is idempotent: ``change`` reports
        ``"new"``, ``"updated"`` or ``"unchanged"``. Indexing is synchronous, so
        ``wait`` only exists for PageIndex compatibility and has no effect.
        """
        return self._local().submit_document(path, name=name, wait=wait)

    def submit_database(
        self,
        path: str | os.PathLike[str],
        *,
        allowed_columns: Mapping[str, Sequence[str]],
        queries: Sequence[object],
        name: str | None = None,
    ) -> dict[str, JsonValue]:
        """Register a SQLite file with the typed, read-only queries you allow."""
        return self._local().submit_database(
            path, allowed_columns=allowed_columns, queries=queries, name=name
        )

    def get_document(self, doc_id: str) -> dict[str, JsonValue]:
        return self._local().get_document(doc_id)

    def list_documents(self, limit: int = 50, offset: int = 0) -> dict[str, JsonValue]:
        """List submitted documents, newest first."""
        return self._local().list_documents(limit, offset)

    def get_document_id(self, name: str) -> str:
        return self._local().get_document_id(name)

    def get_tree(
        self, doc_id: str, *, max_entries: int | None = None
    ) -> dict[str, JsonValue]:
        """The document outline: titles, node IDs, summaries and nested nodes."""
        return self._local().get_tree(doc_id, max_entries=max_entries)

    def get_section(self, doc_id: str, node_id: str) -> dict[str, JsonValue]:
        """The text of one document section, with its native locator."""
        return self._local().get_section(doc_id, node_id)

    def delete_document(self, doc_id: str) -> bool:
        """Remove a document from the workspace; ``False`` if it was not there."""
        return self._local().delete_document(doc_id)

    def chat(
        self,
        question: str,
        doc_id: str | Sequence[str] | None = None,
        *,
        budget: ReasoningBudget | None = None,
        instructions: str | None = None,
    ) -> ChatAnswer:
        """Answer a question from submitted files, citing the evidence used.

        Without ``doc_id`` every submitted document is searched. The answer is
        built only from evidence returned by authorized tools; when none
        supports an answer, the result abstains instead of guessing.
        """
        return self._local().chat(
            question, doc_id, budget=budget, instructions=instructions
        )

    # ------------------------------------------------------------------
    # Agent integration: the same retrieval, driven by your own agent loop

    def agent_tools(self, *, include_management: bool = False) -> list[AgentTool]:
        self._local()
        return list(agent_tool_definitions(include_management))

    def as_openai_tools(
        self, *, include_management: bool = False
    ) -> list[dict[str, JsonValue]]:
        self._local()
        return [
            tool.to_openai() for tool in agent_tool_definitions(include_management)
        ]

    def as_anthropic_tools(
        self, *, include_management: bool = False
    ) -> list[dict[str, JsonValue]]:
        self._local()
        return [
            tool.to_anthropic() for tool in agent_tool_definitions(include_management)
        ]

    def agent_instructions(self, *, include_management: bool = False) -> str:
        self._local()
        return agent_instructions(include_management)

    def call_tool(
        self,
        name: str,
        arguments: str | Mapping[str, object] | None = None,
        *,
        include_management: bool = False,
    ) -> str:
        """Run one tool call from an agent and return JSON text for the model.

        Refused or failed calls return ``{"error": ...}`` so the model can
        recover; the same source and entitlement checks as ``chat`` apply.
        """
        return call_agent_tool(
            self._local(), name, arguments, include_management=include_management
        )

    def list_extensions(self) -> tuple[PluginStatus, ...]:
        self._ensure_open()
        return self._registry.statuses

    def extension(self, plugin_id: str) -> DataIndexExtension:
        self._ensure_open()
        status = next(
            (item for item in self._registry.statuses if item.plugin_id == plugin_id),
            None,
        )
        extension = self._registry.loaded_extensions.get(plugin_id)
        if (
            status is None
            or extension is None
            or not status.compatible
            or not status.trusted
            or not status.enabled
            or status.healthy is not True
        ):
            raise ToolNotAvailableError(
                f"plugin {plugin_id!r} is not trusted, enabled and healthy"
            )
        manifest = extension.manifest()
        if (
            manifest.plugin_id != plugin_id
            or manifest.distribution != status.distribution
            or manifest.plugin_version != status.distribution_version
        ):
            raise ToolNotAvailableError(
                f"plugin {plugin_id!r} changed its runtime manifest"
            )
        if extension.health().status is not HealthStatus.HEALTHY:
            raise ToolNotAvailableError(f"plugin {plugin_id!r} is not healthy")
        return extension

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Callbacks run last-in first-out and all of them run even if one fails:
        # plugins first, then the workspace, then the catalog they all share.
        with ExitStack() as stack:
            stack.callback(self._catalog.close)
            if self._runtime is not None:
                stack.callback(self._runtime.close)
            for extension in self._registry.loaded_extensions.values():
                stack.callback(extension.close)

    def __enter__(self) -> DataIndexClient:
        self._ensure_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _authorize_source(self, source: SourceRef) -> None:
        if self._catalog.get_source(source.source_id) != source:
            raise StaleSourceVersionError(
                f"source {source.source_id!r} is not at the requested revision"
            )
        if self._catalog.get_source_tenant(source.source_id) != self.tenant_id:
            raise SourceAuthorizationError(
                f"source {source.source_id!r} does not belong to this tenant"
            )
        if not self._source_authorizer.is_authorized(
            self.tenant_id, source, "source.access"
        ):
            raise SourceAuthorizationError(
                f"tenant is not authorized for source {source.source_id!r}"
            )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("DataIndexClient is closed")

    def _local(self) -> LocalRuntime:
        self._ensure_open()
        if self._runtime is None:
            raise ConfigurationError(
                "this client was opened with explicit host components and has no "
                "local document workflow; create it with DataIndexClient(chat=...)"
            )
        return self._runtime


def _storage_directory(storage_path: str | os.PathLike[str]) -> Path:
    try:
        text: object = os.fspath(storage_path)
    except TypeError:
        text = None
    if not isinstance(text, str) or not text.strip():
        raise ValueError("storage_path must be a non-empty path")
    return Path(text).expanduser()


def _close_quietly(closers: Iterable[Callable[[], object]]) -> None:
    for closer in closers:
        with suppress(Exception):
            closer()
