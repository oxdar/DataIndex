from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import traceback
import types
from collections.abc import Callable, Iterator, Sequence
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path

import pytest

from dataindex.catalog import SQLiteCatalog
from dataindex.exceptions import (
    CatalogError,
    ChatModelConfigurationError,
    ConfigurationError,
    DocumentNotFoundError,
    SourceAuthorizationError,
    UnsupportedSchemaVersionError,
    WorkspaceError,
)
from dataindex.llm import ChatResult
from dataindex.models import ExtensionManifest, HealthResult, HealthStatus, SourceRef
from dataindex.reasoning import ReasoningBudget
from dataindex.registry import ExtensionRegistry
from dataindex.sdk import DataIndexClient

SOURCE = SourceRef("doc-owned", "text/markdown", "v1")
EMPTY_LISTING = {"documents": [], "total": 0, "limit": 50, "offset": 0}
NO_MODEL = "no chat model is configured"
ALPHA = ("dataindex-ext-alpha", "0.1.0")
BETA = ("dataindex-ext-beta", "0.1.0")


class RecordingModel:
    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self,
        messages: Sequence[object],
        *,
        max_output_tokens: int,
        timeout_seconds: float,
        json_output: bool = True,
    ) -> ChatResult:
        self.calls += 1
        return ChatResult('{"action": "answer", "claims": []}')


class FakeLlmPackage:
    """Stands in for the optional dataindex_llm package."""

    def __init__(self, factory: Callable[..., object] | None = None) -> None:
        self.created: list[tuple[str, dict[str, str]]] = []
        self.model = RecordingModel()
        self._factory = factory

    def create_chat_model(self, name: str, **options: str) -> object:
        self.created.append((name, options))
        return self._factory(name, **options) if self._factory else self.model

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeLlmPackage:
        module = types.ModuleType("dataindex_llm")
        setattr(module, "create_chat_model", self.create_chat_model)
        monkeypatch.setitem(sys.modules, "dataindex_llm", module)
        return self


class AllowAll:
    def is_authorized(self, tenant_id: str, source: SourceRef, capability: str) -> bool:
        return True


def manifest_for(plugin_id: str) -> ExtensionManifest:
    return ExtensionManifest(
        plugin_id=plugin_id,
        distribution=f"dataindex-ext-{plugin_id}",
        plugin_version="0.1.0",
        core_api_range=">=0.1,<0.2",
        edition="free",
        license_mode="none",
        capabilities=(f"{plugin_id}.index",),
        source_types=("text/markdown",),
        publisher="test",
    )


class FixtureExtension:
    def __init__(
        self, manifest: ExtensionManifest, close_error: Exception | None
    ) -> None:
        self._manifest = manifest
        self._close_error = close_error
        self.close_calls = 0

    def manifest(self) -> ExtensionManifest:
        return self._manifest

    def health(self) -> HealthResult:
        return HealthResult(HealthStatus.HEALTHY)

    def close(self) -> None:
        self.close_calls += 1
        if self._close_error is not None:
            raise self._close_error


class FixtureDistribution:
    def __init__(self, manifest: ExtensionManifest) -> None:
        self.name = manifest.distribution
        self.version = manifest.plugin_version
        module = f"dataindex_ext_{manifest.plugin_id}"
        self._files = {
            f"{module}/dataindex-extension.json": json.dumps(asdict(manifest))
        }

    def read_text(self, path: str) -> str | None:
        return self._files.get(path)


class FixtureEntryPoint:
    """A discoverable plugin whose code runs only if the registry loads it."""

    def __init__(
        self,
        plugin_id: str,
        *,
        load_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        manifest = manifest_for(plugin_id)
        self.name = plugin_id
        self.value = f"dataindex_ext_{plugin_id}.plugin:load_plugin"
        self.dist = FixtureDistribution(manifest)
        self.extension = FixtureExtension(manifest, close_error)
        self._load_error = load_error
        self.load_calls = 0

    def load(self) -> Callable[[], FixtureExtension]:
        self.load_calls += 1
        if self._load_error is not None:
            raise self._load_error
        return lambda: self.extension


def install_plugins(
    monkeypatch: pytest.MonkeyPatch, *points: FixtureEntryPoint
) -> None:
    monkeypatch.setattr("dataindex.local.entry_points", lambda *, group: points)
    monkeypatch.setattr("dataindex.registry.entry_points", lambda *, group: points)


@pytest.fixture(autouse=True)
def no_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    install_plugins(monkeypatch)


@pytest.fixture
def bare_client(tmp_path: Path) -> Iterator[DataIndexClient]:
    with DataIndexClient(storage_path=tmp_path / "ws") as client:
        yield client


@pytest.fixture
def model() -> RecordingModel:
    return RecordingModel()


@pytest.fixture
def model_client(tmp_path: Path, model: RecordingModel) -> Iterator[DataIndexClient]:
    with DataIndexClient(chat=model, storage_path=tmp_path / "ws") as client:
        yield client


@pytest.fixture(params=["from_components", "open"])
def host_client(request: pytest.FixtureRequest) -> Iterator[DataIndexClient]:
    if request.param == "from_components":
        client = DataIndexClient.from_components(
            SQLiteCatalog(":memory:"),
            ExtensionRegistry(),
            tenant_id="tenant-a",
            source_authorizer=AllowAll(),
            entitlement_provider=None,
            deployment="host-test",
        )
    else:
        client = DataIndexClient.open(
            tenant_id="tenant-a",
            source_authorizer=AllowAll(),
            deployment="host-test",
        )
    yield client
    client.close()


def seed_catalog(storage: Path, tenant_id: str) -> None:
    storage.mkdir(parents=True)
    catalog = SQLiteCatalog(storage / "catalog.sqlite3")
    try:
        catalog.create_source(SOURCE, tenant_id=tenant_id)
    finally:
        catalog.close()


LOCAL_WORKFLOW: dict[str, Callable[[DataIndexClient], object]] = {
    "submit_document": lambda c: c.submit_document("notes.md"),
    "submit_database": lambda c: c.submit_database(
        "data.sqlite",
        allowed_columns={"t": ["a"]},
        queries=[{"query_id": "q1"}],
    ),
    "get_document": lambda c: c.get_document("doc-1"),
    "list_documents": lambda c: c.list_documents(),
    "get_document_id": lambda c: c.get_document_id("notes"),
    "get_tree": lambda c: c.get_tree("doc-1"),
    "get_section": lambda c: c.get_section("doc-1", "n1"),
    "delete_document": lambda c: c.delete_document("doc-1"),
    "chat": lambda c: c.chat("What is due?"),
    "agent_tools": lambda c: c.agent_tools(),
    "as_openai_tools": lambda c: c.as_openai_tools(),
    "as_anthropic_tools": lambda c: c.as_anthropic_tools(),
    "agent_instructions": lambda c: c.agent_instructions(),
    "call_tool": lambda c: c.call_tool("list_documents"),
}


# --- a client without a chat model -----------------------------------------


@pytest.mark.parametrize("as_text", [False, True], ids=["path", "str"])
def test_a_submit_only_client_needs_no_model_and_creates_its_workspace(
    tmp_path: Path, as_text: bool
) -> None:
    storage = tmp_path / "ws"
    with DataIndexClient(storage_path=str(storage) if as_text else storage) as client:
        assert client.tenant_id == "local"
        assert client.list_documents() == EMPTY_LISTING
        assert client.list_extensions() == ()
    assert (storage / "catalog.sqlite3").is_file()
    assert (storage / "workspace.sqlite3").is_file()


def test_default_storage_is_a_dot_dataindex_folder_in_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    with DataIndexClient() as client:
        assert client.list_documents()["total"] == 0
    assert (tmp_path / ".dataindex" / "catalog.sqlite3").is_file()
    assert (tmp_path / ".dataindex" / "workspace.sqlite3").is_file()


@pytest.mark.parametrize("backend", [{"api_key": "key-1"}, {}], ids=["keys", "empty"])
def test_chat_backend_needs_a_model_name(
    tmp_path: Path, backend: dict[str, str]
) -> None:
    with pytest.raises(ChatModelConfigurationError, match="chat_backend"):
        DataIndexClient(chat_backend=backend, storage_path=tmp_path / "ws")
    assert list(tmp_path.iterdir()) == []


# --- resolving the chat model at construction -------------------------------


def test_a_model_name_without_dataindex_llm_fails_at_construction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(sys.modules, "dataindex_llm", None)
    with pytest.raises(ChatModelConfigurationError, match="dataindex-llm"):
        DataIndexClient(chat="some-model", storage_path=tmp_path / "ws")
    assert list(tmp_path.iterdir()) == []


def test_a_model_name_is_resolved_once_at_construction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    llm = FakeLlmPackage().install(monkeypatch)
    backend = {"api_key": "key-1", "api_base": "http://llm.invalid/v1"}
    with DataIndexClient(
        chat="  fake-model  ", chat_backend=backend, storage_path=tmp_path / "ws"
    ) as client:
        assert llm.created == [("fake-model", backend)]
        client.chat("What is due?")
        client.chat("And later?")
    assert len(llm.created) == 1
    assert llm.model.calls == 0


@pytest.mark.parametrize("name", ["", "   "], ids=["empty", "blank"])
def test_blank_model_names_are_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    llm = FakeLlmPackage().install(monkeypatch)
    with pytest.raises(ChatModelConfigurationError, match="must not be empty"):
        DataIndexClient(chat=name, storage_path=tmp_path / "ws")
    assert llm.created == []


@pytest.mark.parametrize(
    "backend",
    [
        pytest.param({"organization": "org-1"}, id="unsupported-key"),
        pytest.param({"api_key": ""}, id="empty-value"),
        pytest.param({"api_key": 7}, id="non-string-value"),
    ],
)
def test_bad_chat_backends_are_rejected_before_the_adapter_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: dict[str, object]
) -> None:
    llm = FakeLlmPackage().install(monkeypatch)
    with pytest.raises(ChatModelConfigurationError):
        DataIndexClient(
            chat="fake-model",
            chat_backend=backend,  # type: ignore[arg-type]
            storage_path=tmp_path / "ws",
        )
    assert llm.created == []
    assert list(tmp_path.iterdir()) == []


def test_chat_backend_only_applies_to_a_model_name(tmp_path: Path) -> None:
    with pytest.raises(ChatModelConfigurationError, match="model name"):
        DataIndexClient(
            chat=RecordingModel(),
            chat_backend={"api_key": "key-1"},
            storage_path=tmp_path / "ws",
        )
    assert list(tmp_path.iterdir()) == []


def test_a_failing_model_factory_does_not_leak_its_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def create_chat_model(name: str, **options: str) -> object:
        raise RuntimeError("401 for key sk-live-SECRET")

    FakeLlmPackage(create_chat_model).install(monkeypatch)
    with pytest.raises(ChatModelConfigurationError) as caught:
        DataIndexClient(chat="fake-model", storage_path=tmp_path / "ws")
    assert "RuntimeError" in str(caught.value)
    assert "SECRET" not in str(caught.value)
    assert "SECRET" not in "".join(traceback.format_exception(caught.value))
    assert list(tmp_path.iterdir()) == []


def test_a_chat_model_error_from_the_adapter_is_reported_as_is(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def create_chat_model(name: str, **options: str) -> object:
        raise ChatModelConfigurationError("set OPENAI_API_KEY first")

    FakeLlmPackage(create_chat_model).install(monkeypatch)
    with pytest.raises(ChatModelConfigurationError, match="set OPENAI_API_KEY first"):
        DataIndexClient(chat="fake-model", storage_path=tmp_path / "ws")


def test_an_adapter_package_without_a_factory_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(sys.modules, "dataindex_llm", types.ModuleType("dataindex_llm"))
    with pytest.raises(ChatModelConfigurationError, match="create_chat_model"):
        DataIndexClient(chat="fake-model", storage_path=tmp_path / "ws")
    assert list(tmp_path.iterdir()) == []


def test_a_factory_that_returns_no_model_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    FakeLlmPackage(lambda name, **options: object()).install(monkeypatch)
    with pytest.raises(ChatModelConfigurationError, match="did not return"):
        DataIndexClient(chat="fake-model", storage_path=tmp_path / "ws")
    assert list(tmp_path.iterdir()) == []


def test_a_model_object_passes_through_without_importing_dataindex_llm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, model: RecordingModel
) -> None:
    monkeypatch.setitem(sys.modules, "dataindex_llm", None)
    with DataIndexClient(chat=model, storage_path=tmp_path / "ws") as client:
        assert client.chat("What is due?").abstained
    assert model.calls == 0


@pytest.mark.parametrize(
    "chat",
    [
        pytest.param(123, id="integer"),
        pytest.param(object(), id="plain-object"),
        pytest.param(
            types.SimpleNamespace(complete="nope"), id="complete-not-callable"
        ),
        pytest.param(types.SimpleNamespace(complete=None), id="complete-none"),
    ],
)
def test_other_chat_values_are_rejected(tmp_path: Path, chat: object) -> None:
    with pytest.raises(ChatModelConfigurationError, match="complete"):
        DataIndexClient(chat=chat, storage_path=tmp_path / "ws")  # type: ignore[arg-type]
    assert list(tmp_path.iterdir()) == []


# --- option validation: nothing is created when an option is rejected --------

INVALID_OPTIONS = [
    pytest.param(
        {"trusted_plugins": "dataindex-ext-documents"},
        ConfigurationError,
        "single string",
        id="trusted-lone-string",
    ),
    pytest.param(
        {"trusted_plugins": b"dataindex-ext-documents"},
        ConfigurationError,
        "single string",
        id="trusted-bytes",
    ),
    pytest.param(
        {"trusted_plugins": ["dataindex-ext-does-not-exist"]},
        ConfigurationError,
        "not installed",
        id="trusted-not-installed",
    ),
    pytest.param(
        {"trusted_plugins": [""]},
        ConfigurationError,
        "distribution name",
        id="trusted-empty-name",
    ),
    pytest.param(
        {"trusted_plugins": ["  "]},
        ConfigurationError,
        "distribution name",
        id="trusted-blank-name",
    ),
    pytest.param(
        {"trusted_plugins": [42]},
        ConfigurationError,
        "distribution name",
        id="trusted-integer",
    ),
    pytest.param(
        {"trusted_plugins": [("name-only",)]},
        ConfigurationError,
        "pair",
        id="trusted-short-pair",
    ),
    pytest.param(
        {"trusted_plugins": [("a", "1", "extra")]},
        ConfigurationError,
        "pair",
        id="trusted-long-pair",
    ),
    pytest.param(
        {"trusted_plugins": [("a", 1)]},
        ConfigurationError,
        "pair",
        id="trusted-non-string-version",
    ),
    pytest.param(
        {"trusted_plugins": [("dataindex-ext-a", "not.a.version")]},
        ConfigurationError,
        "invalid version",
        id="trusted-bad-version",
    ),
    pytest.param(
        {"instructions": 123}, ValueError, "instructions", id="instructions-integer"
    ),
    pytest.param(
        {"instructions": ["be brief"]},
        ValueError,
        "instructions",
        id="instructions-list",
    ),
    pytest.param(
        {"instructions": "x" * 4001}, ValueError, "4000", id="instructions-too-long"
    ),
    pytest.param({"tenant_id": ""}, ValueError, "tenant_id", id="tenant-empty"),
    pytest.param({"tenant_id": "   "}, ValueError, "tenant_id", id="tenant-blank"),
    pytest.param({"tenant_id": None}, ValueError, "tenant_id", id="tenant-none"),
    pytest.param({"tenant_id": 7}, ValueError, "tenant_id", id="tenant-integer"),
    pytest.param({"storage_path": ""}, ValueError, "storage_path", id="storage-empty"),
    pytest.param(
        {"storage_path": "  "}, ValueError, "storage_path", id="storage-blank"
    ),
    pytest.param({"storage_path": None}, ValueError, "storage_path", id="storage-none"),
    pytest.param({"storage_path": 5}, ValueError, "storage_path", id="storage-integer"),
    pytest.param(
        {"storage_path": b"ws"}, ValueError, "storage_path", id="storage-bytes"
    ),
]


@pytest.mark.parametrize(("options", "error", "message"), INVALID_OPTIONS)
def test_invalid_options_are_rejected_before_anything_is_created(
    tmp_path: Path, options: dict[str, object], error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        DataIndexClient(**{"storage_path": tmp_path / "ws", **options})  # type: ignore[arg-type]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "options",
    [
        pytest.param({"trusted_plugins": []}, id="trust-nothing"),
        pytest.param(
            {"trusted_plugins": [("dataindex-ext-uninstalled", "1.2.3")]},
            id="pinned-pair",
        ),
        pytest.param({"instructions": "   "}, id="blank-instructions"),
        pytest.param({"instructions": "x" * 4000}, id="longest-instructions"),
        pytest.param({"tenant_id": "alice"}, id="custom-tenant"),
    ],
)
def test_edge_options_that_are_valid_are_accepted(
    tmp_path: Path, options: dict[str, object]
) -> None:
    with DataIndexClient(storage_path=tmp_path / "ws", **options) as client:  # type: ignore[arg-type]
        assert client.list_documents() == EMPTY_LISTING


# --- which plugins a client trusts and loads --------------------------------


@pytest.mark.parametrize(
    "trusted",
    [
        pytest.param(None, id="default"),
        pytest.param([], id="nothing"),
        pytest.param([("dataindex-ext-alpha", "9.9.9")], id="other-version"),
        pytest.param([("dataindex-ext-other", "0.1.0")], id="other-distribution"),
    ],
)
def test_a_plugin_that_is_not_exactly_trusted_is_never_loaded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    trusted: list[tuple[str, str]] | None,
) -> None:
    point = FixtureEntryPoint("alpha")
    install_plugins(monkeypatch, point)
    with DataIndexClient(
        storage_path=tmp_path / "ws", trusted_plugins=trusted
    ) as client:
        (status,) = client.list_extensions()
        assert status.plugin_id == "alpha"
        assert (status.trusted, status.enabled, status.healthy) == (False, False, None)
    assert point.load_calls == 0


def test_a_trusted_plugin_is_loaded_and_closed_with_the_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    point = FixtureEntryPoint("alpha")
    install_plugins(monkeypatch, point)
    with DataIndexClient(
        storage_path=tmp_path / "ws", trusted_plugins=[ALPHA]
    ) as client:
        (status,) = client.list_extensions()
        assert (status.trusted, status.enabled, status.healthy) == (True, True, True)
        assert point.extension.close_calls == 0
    assert point.load_calls == 1
    assert point.extension.close_calls == 1


def test_a_plugin_that_fails_to_load_does_not_stop_the_client_or_the_others(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    good = FixtureEntryPoint("alpha")
    broken = FixtureEntryPoint("beta", load_error=RuntimeError("boom"))
    install_plugins(monkeypatch, good, broken)
    with DataIndexClient(
        storage_path=tmp_path / "ws", trusted_plugins=[ALPHA, BETA]
    ) as client:
        statuses = {status.plugin_id: status for status in client.list_extensions()}
        assert statuses["alpha"].healthy is True
        assert statuses["beta"].healthy is False
        assert client.list_documents() == EMPTY_LISTING
    assert good.extension.close_calls == 1


def test_close_releases_every_plugin_and_the_storage_even_if_one_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    storage = tmp_path / "ws"
    first = FixtureEntryPoint("alpha")
    second = FixtureEntryPoint("beta", close_error=RuntimeError("close failed"))
    install_plugins(monkeypatch, first, second)
    client = DataIndexClient(storage_path=storage, trusted_plugins=[ALPHA, BETA])
    with suppress(RuntimeError):
        client.close()
    client.close()
    assert first.extension.close_calls == 1
    assert second.extension.close_calls == 1
    shutil.rmtree(storage)


@pytest.mark.parametrize(
    ("trusted", "enabled", "loaded"),
    [
        pytest.param((), ("alpha",), False, id="enabled-but-not-trusted"),
        pytest.param((ALPHA,), (), False, id="trusted-but-not-enabled"),
        pytest.param((ALPHA,), ("alpha",), True, id="trusted-and-enabled"),
    ],
)
def test_a_host_client_loads_a_plugin_only_when_trusted_and_enabled(
    monkeypatch: pytest.MonkeyPatch,
    trusted: tuple[tuple[str, str], ...],
    enabled: tuple[str, ...],
    loaded: bool,
) -> None:
    point = FixtureEntryPoint("alpha")
    install_plugins(monkeypatch, point)
    client = DataIndexClient.open(
        tenant_id="tenant-a",
        source_authorizer=AllowAll(),
        trusted_distributions=trusted,
        enabled_plugin_ids=enabled,
        deployment="host-test",
    )
    try:
        assert point.load_calls == (1 if loaded else 0)
        (status,) = client.list_extensions()
        assert status.healthy is (True if loaded else None)
    finally:
        client.close()


# --- lifecycle ---------------------------------------------------------------


def test_leaving_the_with_block_closes_the_client_and_releases_the_storage(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "ws"
    with DataIndexClient(storage_path=storage) as client:
        assert client.list_documents()["total"] == 0
    with pytest.raises(RuntimeError, match="closed"):
        client.list_documents()
    shutil.rmtree(storage)
    assert not storage.exists()


def test_the_client_is_closed_when_the_with_body_raises(tmp_path: Path) -> None:
    storage = tmp_path / "ws"
    with pytest.raises(KeyError), DataIndexClient(storage_path=storage) as client:
        raise KeyError("body failed")
    with pytest.raises(RuntimeError, match="closed"):
        client.list_documents()
    shutil.rmtree(storage)


@pytest.mark.parametrize("name", list(LOCAL_WORKFLOW))
def test_every_workflow_method_refuses_a_closed_client(
    tmp_path: Path, name: str
) -> None:
    client = DataIndexClient(storage_path=tmp_path / "ws")
    client.close()
    client.close()
    with pytest.raises(RuntimeError, match="closed"):
        LOCAL_WORKFLOW[name](client)


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda c: c.list_sources(), id="list_sources"),
        pytest.param(lambda c: c.get_source("doc-1"), id="get_source"),
        pytest.param(lambda c: c.list_extensions(), id="list_extensions"),
        pytest.param(lambda c: c.__enter__(), id="enter"),
    ],
)
def test_the_host_surface_refuses_a_closed_client_too(
    tmp_path: Path, call: Callable[[DataIndexClient], object]
) -> None:
    client = DataIndexClient(storage_path=tmp_path / "ws")
    client.close()
    with pytest.raises(RuntimeError, match="closed"):
        call(client)


def test_storage_can_be_reopened_after_the_client_closes(tmp_path: Path) -> None:
    storage = tmp_path / "ws"
    for _ in range(2):
        with DataIndexClient(storage_path=storage) as client:
            assert client.list_documents() == EMPTY_LISTING


def test_a_corrupt_workspace_file_fails_construction_and_leaks_no_handle(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "ws"
    storage.mkdir()
    (storage / "workspace.sqlite3").write_bytes(b"this is not a database" * 50)
    with pytest.raises(WorkspaceError, match="corrupt"):
        DataIndexClient(storage_path=storage)
    shutil.rmtree(storage)


def test_a_workspace_from_a_newer_release_is_refused_with_an_upgrade_hint(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "ws"
    storage.mkdir()
    connection = sqlite3.connect(storage / "workspace.sqlite3")
    connection.execute("PRAGMA user_version = 99")
    connection.commit()
    connection.close()
    with pytest.raises(WorkspaceError, match="upgrade dataindex-core"):
        DataIndexClient(storage_path=storage)
    shutil.rmtree(storage)


def test_a_corrupt_catalog_file_fails_construction_and_leaks_no_handle(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "ws"
    storage.mkdir()
    (storage / "catalog.sqlite3").write_bytes(b"this is not a database" * 50)
    with pytest.raises(CatalogError, match="corrupt") as failure:
        DataIndexClient(storage_path=storage)
    # The caught error keeps the failed constructor's frames alive, so only an
    # explicit close, not garbage collection, can have released the file.
    shutil.rmtree(storage)
    assert str(storage) not in str(failure.value)


def test_a_catalog_from_a_newer_release_fails_construction_and_leaks_no_handle(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "ws"
    storage.mkdir()
    connection = sqlite3.connect(storage / "catalog.sqlite3")
    connection.execute("PRAGMA user_version = 99")
    connection.commit()
    connection.close()
    with pytest.raises(UnsupportedSchemaVersionError, match="newer") as failure:
        DataIndexClient(storage_path=storage)
    # The caught error keeps the failed constructor's frames alive, so only an
    # explicit close, not garbage collection, can have released the file.
    shutil.rmtree(storage)
    assert failure.type is UnsupportedSchemaVersionError


def test_a_host_client_releases_its_catalog_file_on_close(tmp_path: Path) -> None:
    path = tmp_path / "host.sqlite"
    client = DataIndexClient.open(
        path,
        tenant_id="tenant-a",
        source_authorizer=AllowAll(),
        deployment="host-test",
    )
    client.close()
    client.close()
    path.unlink()


# --- host clients have no local workflow ------------------------------------


@pytest.mark.parametrize("name", list(LOCAL_WORKFLOW))
def test_host_clients_refuse_the_local_workflow(
    host_client: DataIndexClient, name: str
) -> None:
    with pytest.raises(ConfigurationError, match="no local document workflow"):
        LOCAL_WORKFLOW[name](host_client)


def test_host_clients_keep_their_own_surface(host_client: DataIndexClient) -> None:
    assert host_client.tenant_id == "tenant-a"
    assert host_client.list_sources() == ()
    assert host_client.list_extensions() == ()


def test_a_closed_host_client_raises_runtime_error_not_a_configuration_error(
    host_client: DataIndexClient,
) -> None:
    host_client.close()
    with pytest.raises(RuntimeError, match="closed"):
        host_client.chat("What is due?")
    with pytest.raises(RuntimeError, match="closed"):
        host_client.list_sources()


@pytest.mark.parametrize("tenant_id", ["", "   ", None, 7])
def test_host_constructors_require_a_tenant(tenant_id: object) -> None:
    catalog = SQLiteCatalog(":memory:")
    try:
        with pytest.raises(ValueError, match="tenant_id"):
            DataIndexClient.from_components(
                catalog,
                ExtensionRegistry(),
                tenant_id=tenant_id,  # type: ignore[arg-type]
                source_authorizer=AllowAll(),
                entitlement_provider=None,
                deployment="host-test",
            )
    finally:
        catalog.close()
    with pytest.raises(ValueError, match="tenant_id"):
        DataIndexClient.open(
            tenant_id=tenant_id,  # type: ignore[arg-type]
            source_authorizer=AllowAll(),
            deployment="host-test",
        )


@pytest.mark.parametrize("deployment", ["", "  ", None])
def test_open_requires_a_deployment_name(deployment: object) -> None:
    with pytest.raises(ValueError, match="deployment"):
        DataIndexClient.open(
            tenant_id="tenant-a",
            source_authorizer=AllowAll(),
            deployment=deployment,  # type: ignore[arg-type]
        )


# --- chat(): validation order -------------------------------------------------


@pytest.mark.parametrize(
    "question",
    ["", "   ", "\n\t", None, 7, b"question", ["question"], "x" * 2049],
    ids=["empty", "blank", "whitespace", "none", "int", "bytes", "list", "too-long"],
)
def test_chat_rejects_a_bad_question_before_anything_else(
    bare_client: DataIndexClient, question: object
) -> None:
    with pytest.raises(ValueError, match="question must be a string of 1 to 2048"):
        bare_client.chat(question, doc_id=[], budget="nope", instructions=5)  # type: ignore[arg-type]


@pytest.mark.parametrize("budget", ["big", 5, {"max_depth": 1}, object()])
def test_chat_rejects_a_non_budget_before_instructions_model_and_targets(
    bare_client: DataIndexClient, budget: object
) -> None:
    with pytest.raises(ValueError, match="budget must be a ReasoningBudget"):
        bare_client.chat("What is due?", doc_id=[], budget=budget, instructions=5)  # type: ignore[arg-type]


@pytest.mark.parametrize("instructions", [5, ["be brief"], "y" * 4001])
def test_chat_rejects_bad_instructions_before_the_model_and_targets(
    bare_client: DataIndexClient, instructions: object
) -> None:
    with pytest.raises(ValueError, match="instructions"):
        bare_client.chat("What is due?", doc_id=[], instructions=instructions)  # type: ignore[arg-type]


def test_chat_without_a_model_fails_even_on_an_empty_workspace(
    bare_client: DataIndexClient,
) -> None:
    with pytest.raises(ChatModelConfigurationError, match=NO_MODEL):
        bare_client.chat("What is due?")


def test_chat_checks_the_model_before_validating_the_targets(
    bare_client: DataIndexClient,
) -> None:
    with pytest.raises(ChatModelConfigurationError, match=NO_MODEL):
        bare_client.chat("What is due?", doc_id=[""])


@pytest.mark.parametrize(
    "doc_id",
    [
        pytest.param("", id="empty-string"),
        pytest.param([], id="empty-list"),
        pytest.param([""], id="empty-item"),
        pytest.param([7], id="non-string-item"),
        pytest.param(7, id="integer"),
        pytest.param(b"doc-1", id="bytes"),
        pytest.param({"doc-1": 1}, id="mapping"),
        pytest.param([f"doc-{number}" for number in range(65)], id="too-many"),
    ],
)
def test_chat_rejects_bad_targets_without_calling_the_model(
    model_client: DataIndexClient, model: RecordingModel, doc_id: object
) -> None:
    with pytest.raises(ValueError, match="doc_id"):
        model_client.chat("What is due?", doc_id)  # type: ignore[arg-type]
    assert model.calls == 0


def test_chat_with_an_unknown_explicit_document_names_at_most_64_characters(
    model_client: DataIndexClient, model: RecordingModel
) -> None:
    with pytest.raises(DocumentNotFoundError) as caught:
        model_client.chat("What is due?", "d" * 200)
    assert "d" * 64 in str(caught.value)
    assert "d" * 65 not in str(caught.value)
    assert model.calls == 0


def test_chat_over_an_empty_workspace_abstains_without_calling_the_model(
    model_client: DataIndexClient, model: RecordingModel
) -> None:
    answer = model_client.chat("What is due?")
    assert answer.abstained is True
    assert "submitted" in (answer.reason or "")
    assert (answer.claims, answer.citations, answer.sources) == ((), (), ())
    assert model.calls == 0


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"question": "q" * 2048}, id="longest-question"),
        pytest.param({"instructions": "   "}, id="blank-instructions"),
        pytest.param({"instructions": "y" * 4000}, id="longest-instructions"),
        pytest.param({"budget": ReasoningBudget(max_depth=1)}, id="custom-budget"),
    ],
)
def test_chat_accepts_valid_edge_arguments(
    model_client: DataIndexClient, model: RecordingModel, arguments: dict[str, object]
) -> None:
    question = arguments.pop("question", "What is due?")
    answer = model_client.chat(question, **arguments)  # type: ignore[arg-type]
    assert answer.abstained
    assert model.calls == 0


# --- tenants and authorization in local mode --------------------------------


def test_a_different_tenant_cannot_see_or_search_the_stored_tenants_sources(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "ws"
    seed_catalog(storage, tenant_id="alice")
    with DataIndexClient(storage_path=storage, tenant_id="bob") as client:
        assert client.get_source(SOURCE.source_id) is None
        assert client.list_sources() == ()
        with pytest.raises(SourceAuthorizationError, match="tenant"):
            client.search(SOURCE, "anything")


def test_owning_a_source_does_not_grant_access_in_local_mode(tmp_path: Path) -> None:
    storage = tmp_path / "ws"
    seed_catalog(storage, tenant_id="alice")
    with DataIndexClient(storage_path=storage, tenant_id="alice") as client:
        assert client.get_source(SOURCE.source_id) is None
        assert client.list_sources() == ()
        with pytest.raises(SourceAuthorizationError):
            client.search(SOURCE, "anything")


def test_a_local_client_cannot_register_a_source_it_was_not_given(
    tmp_path: Path,
) -> None:
    with DataIndexClient(storage_path=tmp_path / "ws") as client:
        with pytest.raises(SourceAuthorizationError):
            client.register_source(SOURCE)
        assert client.list_sources() == ()


# --- agent tool wrappers -------------------------------------------------------


def test_the_agent_tool_wrappers_honor_include_management(
    bare_client: DataIndexClient,
) -> None:
    read_only = [tool.name for tool in bare_client.agent_tools()]
    managing = [tool.name for tool in bare_client.agent_tools(include_management=True)]
    assert "list_documents" in read_only
    assert "remove_document" not in read_only
    assert "remove_document" in managing

    openai = bare_client.as_openai_tools(include_management=True)
    assert [item["function"]["name"] for item in openai] == managing  # type: ignore[index]
    anthropic = bare_client.as_anthropic_tools()
    assert [item["name"] for item in anthropic] == read_only
    assert "remove_document" in bare_client.agent_instructions(include_management=True)
    assert "remove_document" not in bare_client.agent_instructions()


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda c: c.agent_tools(include_management="yes"), id="agent_tools"
        ),
        pytest.param(lambda c: c.as_openai_tools(include_management=1), id="openai"),
        pytest.param(
            lambda c: c.as_anthropic_tools(include_management="no"), id="anthropic"
        ),
        pytest.param(
            lambda c: c.agent_instructions(include_management=None), id="instructions"
        ),
        pytest.param(
            lambda c: c.call_tool("list_documents", include_management=1),
            id="call_tool",
        ),
    ],
)
def test_include_management_must_be_a_real_boolean(
    bare_client: DataIndexClient, call: Callable[[DataIndexClient], object]
) -> None:
    with pytest.raises(ValueError, match="include_management"):
        call(bare_client)


def test_call_tool_answers_with_json_text_and_gates_management(
    bare_client: DataIndexClient,
) -> None:
    listing = bare_client.call_tool("list_documents", {"limit": 5})
    assert json.loads(listing) == {"documents": [], "total": 0, "limit": 5, "offset": 0}

    refused = json.loads(bare_client.call_tool("remove_document", {"doc_id": "doc-1"}))
    assert "error" in refused
    allowed = json.loads(
        bare_client.call_tool(
            "remove_document", {"doc_id": "doc-1"}, include_management=True
        )
    )
    assert allowed == {"doc_id": "doc-1", "removed": False}
