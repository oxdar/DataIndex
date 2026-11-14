"""Model-name routing and agreement with the dataindex-core contract."""

from __future__ import annotations

import inspect
import sys
import tomllib
import types
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from typing import Any

import dataindex
import pytest
from dataindex.exceptions import ChatModelConfigurationError
from dataindex.llm import ChatModel
from llm_fakes import MESSAGES, FakeServer, call

import dataindex_llm
from dataindex_llm import (
    LiteLLMChatModel,
    OpenAICompatibleChatModel,
    create_chat_model,
)

KEY = "test-key-123"
LOOPBACK = "http://127.0.0.1:9/v1"
PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def install_fake_litellm(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    module = types.ModuleType("litellm")

    def completion(**arguments: Any) -> dict[str, Any]:
        calls.append(arguments)
        return {"choices": [{"message": {"content": "{}"}}]}

    setattr(module, "completion", completion)
    monkeypatch.setitem(sys.modules, "litellm", module)
    return calls


def signature_shape(function: Callable[..., Any]) -> list[tuple[Any, ...]]:
    parameters = inspect.signature(function).parameters.values()
    return [(item.name, item.kind, item.default) for item in parameters]


@pytest.mark.parametrize(
    ("name", "sent"),
    [
        ("gpt-4o", "gpt-4o"),
        ("  gpt-4o  ", "gpt-4o"),
        ("openai/gpt-4o", "gpt-4o"),
        (" openai/gpt-4o ", "gpt-4o"),
        ("openai/openai/gpt-4o", "openai/gpt-4o"),
        ("anthropic/claude-sonnet-4", "anthropic/claude-sonnet-4"),
        ("meta-llama/Llama-3.1-8B-Instruct", "meta-llama/Llama-3.1-8B-Instruct"),
        ("llama3:8b", "llama3:8b"),
        ("OpenAI/gpt-4o", "OpenAI/gpt-4o"),
        ("litellm/gpt-4o", "litellm/gpt-4o"),
    ],
)
def test_names_use_the_openai_compatible_backend(name: str, sent: str) -> None:
    model = create_chat_model(name, api_base=LOOPBACK)

    assert isinstance(model, OpenAICompatibleChatModel)
    assert model.model == sent


@pytest.mark.parametrize(
    ("name", "target"),
    [
        ("litellm:gpt-4o", "gpt-4o"),
        ("litellm:openai/gpt-4o", "openai/gpt-4o"),
        (" litellm: anthropic/claude-x ", "anthropic/claude-x"),
    ],
)
def test_the_litellm_prefix_selects_the_litellm_backend(
    monkeypatch: pytest.MonkeyPatch, name: str, target: str
) -> None:
    calls = install_fake_litellm(monkeypatch)

    model = create_chat_model(name, api_key=KEY, api_base="https://gateway.example/v1")
    model.complete(MESSAGES, max_output_tokens=16, timeout_seconds=5)

    assert isinstance(model, LiteLLMChatModel)
    assert calls[0]["model"] == target
    assert calls[0]["api_key"] == KEY
    assert calls[0]["api_base"] == "https://gateway.example/v1"


def test_the_litellm_backend_reports_a_missing_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "litellm", None)

    with pytest.raises(ChatModelConfigurationError, match=r"dataindex-llm\[litellm\]"):
        create_chat_model("litellm:gpt-4o")


def test_key_and_base_url_reach_the_openai_compatible_backend(
    server: FakeServer,
) -> None:
    call(create_chat_model("gpt-4o", api_key=KEY, api_base=server.api_base))

    (request,) = server.requests
    assert request.path == "/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.json["model"] == "gpt-4o"


def test_the_default_endpoint_needs_a_key() -> None:
    with pytest.raises(ChatModelConfigurationError, match="OPENAI_API_KEY"):
        create_chat_model("gpt-4o")


@pytest.mark.parametrize(
    "name",
    ["", "   ", "litellm:", "litellm:   ", "openai/", "openai/  ", None, 5],
)
def test_blank_names_are_rejected(name: Any) -> None:
    with pytest.raises(ChatModelConfigurationError):
        create_chat_model(name, api_base=LOOPBACK)


@pytest.mark.parametrize(
    "arguments",
    [
        {"temperature": 0.2},
        {"environ": {}},
        {"module": object()},
        {"max_response_bytes": 10},
    ],
)
def test_unknown_keywords_are_a_type_error(arguments: dict[str, Any]) -> None:
    with pytest.raises(TypeError):
        create_chat_model("gpt-4o", **arguments)


def test_options_are_keyword_only() -> None:
    with pytest.raises(TypeError):
        create_chat_model("gpt-4o", KEY)  # type: ignore[misc]

    parameters = inspect.signature(create_chat_model).parameters
    assert [item.name for item in parameters.values()] == [
        "model",
        "api_key",
        "api_base",
    ]


@pytest.mark.parametrize("backend", [OpenAICompatibleChatModel, LiteLLMChatModel])
def test_backends_match_the_core_chat_model_protocol(backend: type) -> None:
    assert signature_shape(backend.complete) == signature_shape(ChatModel.complete)


def test_created_models_expose_a_callable_complete() -> None:
    model = create_chat_model("gpt-4o", api_base=LOOPBACK)

    assert callable(getattr(model, "complete", None))


def project_table() -> dict[str, Any]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]


def test_version_matches_the_project_metadata() -> None:
    assert dataindex_llm.__version__ == project_table()["version"]


def test_installed_distribution_has_the_same_version() -> None:
    try:
        installed = metadata.version("dataindex-llm")
    except metadata.PackageNotFoundError:
        pytest.skip("dataindex-llm is not installed; running from the source tree")

    assert installed == dataindex_llm.__version__


def test_declared_core_range_admits_the_core_in_use() -> None:
    requirements = pytest.importorskip("packaging.requirements")
    dependencies = project_table()["dependencies"]
    parsed = [requirements.Requirement(item) for item in dependencies]
    (core,) = [item for item in parsed if item.name == "dataindex-core"]

    assert core.specifier.contains(dataindex.__version__)
