"""The optional LiteLLM backend, exercised with an injected stand-in module."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from dataindex.exceptions import ChatModelConfigurationError, ChatModelError
from llm_fakes import MESSAGES

from dataindex_llm import LiteLLMChatModel

KEY = "test-key-123"
SECRET = "sk-secret-do-not-leak"


def reply(content: Any = "hello", *, total: Any = 7) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(total_tokens=total),
    )


def fake_module(
    response: Any = None, error: BaseException | None = None
) -> tuple[SimpleNamespace, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def completion(**arguments: Any) -> Any:
        calls.append(arguments)
        if error is not None:
            raise error
        return response

    return SimpleNamespace(completion=completion), calls


def build(module: Any, **kwargs: Any) -> LiteLLMChatModel:
    return LiteLLMChatModel("provider/model", module=module, **kwargs)


def run(model: LiteLLMChatModel, **kwargs: Any) -> Any:
    kwargs.setdefault("max_output_tokens", 128)
    kwargs.setdefault("timeout_seconds", 9)
    return model.complete(MESSAGES, **kwargs)


class ProviderError(Exception):
    def __init__(self, message: str, status_code: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class Exploding:
    @property
    def choices(self) -> Any:
        raise RuntimeError(SECRET)


class ExplodingStatus(Exception):
    @property
    def status_code(self) -> int:
        raise RuntimeError(SECRET)


def test_arguments_are_mapped_for_litellm() -> None:
    module, calls = fake_module(reply("hi", total=7))
    model = build(module, api_key=KEY, api_base="https://gateway.example/v1")

    result = run(model)

    assert calls == [
        {
            "model": "provider/model",
            "messages": [
                {"role": "system", "content": "Answer in JSON."},
                {"role": "user", "content": "What is 2+2?"},
            ],
            "max_tokens": 128,
            "timeout": 9.0,
            "temperature": 0,
            "drop_params": True,
            "num_retries": 0,
            "response_format": {"type": "json_object"},
            "api_key": KEY,
            "api_base": "https://gateway.example/v1",
        }
    ]
    assert (result.text, result.tokens_used) == ("hi", 7)


def test_credentials_are_only_sent_when_provided() -> None:
    module, calls = fake_module(reply())

    run(build(module))

    assert "api_key" not in calls[0]
    assert "api_base" not in calls[0]


def test_json_output_false_omits_response_format() -> None:
    module, calls = fake_module(reply())

    run(build(module), json_output=False)

    assert "response_format" not in calls[0]


def test_surrounding_whitespace_is_removed_from_settings() -> None:
    module, calls = fake_module(reply())
    model = LiteLLMChatModel(
        " provider/model ",
        api_key=f" {KEY} ",
        api_base=" https://gateway.example ",
        module=module,
    )

    run(model)

    assert calls[0]["model"] == "provider/model"
    assert calls[0]["api_key"] == KEY
    assert calls[0]["api_base"] == "https://gateway.example"


@pytest.mark.parametrize(
    "response",
    [
        reply("x", total=3),
        {"choices": [{"message": {"content": "x"}}], "usage": {"total_tokens": 3}},
        SimpleNamespace(
            choices=[{"message": SimpleNamespace(content="x")}],
            usage={"total_tokens": 3},
        ),
        {"choices": ({"message": {"content": "x"}},), "usage": {"total_tokens": 3}},
    ],
)
def test_attribute_and_mapping_responses_are_read(response: Any) -> None:
    module, _ = fake_module(response)

    result = run(build(module))

    assert (result.text, result.tokens_used) == ("x", 3)


def test_empty_text_is_a_valid_reply() -> None:
    module, _ = fake_module(reply("", total=0))

    result = run(build(module))

    assert (result.text, result.tokens_used) == ("", 0)


@pytest.mark.parametrize("total", [None, -1, True, "7", 1.5])
def test_unusable_token_counts_become_none(total: Any) -> None:
    module, _ = fake_module(reply(total=total))

    assert run(build(module)).tokens_used is None


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="x"))]
        ),
        {"choices": [{"message": {"content": "x"}}], "usage": {"prompt_tokens": 1}},
        {"choices": [{"message": {"content": "x"}}], "usage": "many"},
    ],
)
def test_missing_usage_gives_no_token_count(response: Any) -> None:
    module, _ = fake_module(response)

    assert run(build(module)).tokens_used is None


@pytest.mark.parametrize(
    "response",
    [
        reply(None),
        reply(5),
        reply(["x"]),
        reply({"text": "x"}),
        SimpleNamespace(choices=[]),
        SimpleNamespace(choices=None),
        SimpleNamespace(choices="nope"),
        {"choices": [{}]},
        {"choices": [None]},
        {"choices": [{"message": None}]},
        {},
        "text",
        None,
    ],
)
def test_responses_without_text_are_an_error(response: Any) -> None:
    module, _ = fake_module(response)

    with pytest.raises(ChatModelError, match="returned no text content"):
        run(build(module))


def test_an_unreadable_response_is_reported_without_its_text() -> None:
    module, _ = fake_module(Exploding())

    with pytest.raises(ChatModelError, match="could not be read") as info:
        run(build(module))

    assert SECRET not in str(info.value)
    assert info.value.__cause__ is None


def test_litellm_failures_are_reported_by_class_name_only() -> None:
    failure = ProviderError(f"Incorrect API key provided: {SECRET}", status_code=401)
    module, _ = fake_module(error=failure)

    with pytest.raises(ChatModelError) as info:
        run(build(module, api_key=SECRET))

    assert info.value.args == ("LiteLLM request failed: ProviderError",)
    assert info.value.status_code == 401
    assert SECRET not in f"{info.value!s} {info.value!r}"
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__


@pytest.mark.parametrize("status", [None, "401", True, 4.5])
def test_only_integer_status_codes_are_kept(status: Any) -> None:
    module, _ = fake_module(error=ProviderError("boom", status_code=status))

    with pytest.raises(ChatModelError) as info:
        run(build(module))

    assert not hasattr(info.value, "status_code")


@pytest.mark.parametrize("failure", [ExplodingStatus(SECRET), ValueError(SECRET)])
def test_failures_without_a_readable_status_code_are_still_mapped(
    failure: Exception,
) -> None:
    module, _ = fake_module(error=failure)

    with pytest.raises(ChatModelError) as info:
        run(build(module))

    assert not hasattr(info.value, "status_code")
    assert SECRET not in str(info.value)


def test_keyboard_interrupts_are_not_swallowed() -> None:
    module, _ = fake_module(error=KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        run(build(module))


def patch_import(
    monkeypatch: pytest.MonkeyPatch, failure: Callable[[], BaseException]
) -> None:
    real_import = importlib.import_module

    def import_module(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "litellm":
            raise failure()
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", import_module)


def test_a_missing_litellm_is_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_import(monkeypatch, lambda: ImportError("No module named 'litellm'"))

    with pytest.raises(ChatModelConfigurationError) as info:
        LiteLLMChatModel("gpt-4o")

    assert str(info.value) == (
        'LiteLLM is not installed; run: pip install "dataindex-llm[litellm]"'
    )
    assert isinstance(info.value.__cause__, ImportError)


def test_other_import_failures_are_reported_by_class_name_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_import(monkeypatch, lambda: RuntimeError(SECRET))

    with pytest.raises(ChatModelConfigurationError) as info:
        LiteLLMChatModel("gpt-4o")

    assert str(info.value) == "LiteLLM could not be imported (RuntimeError)"
    assert info.value.__cause__ is None


def capture_mode_at_import(
    monkeypatch: pytest.MonkeyPatch, outcome: Callable[[], Any]
) -> list[str | None]:
    modes: list[str | None] = []
    real_import = importlib.import_module

    def import_module(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "litellm":
            modes.append(os.environ.get("LITELLM_MODE"))
            return outcome()
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", import_module)
    return modes


def test_litellm_is_imported_without_loading_a_dotenv_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LITELLM_MODE", raising=False)
    module, _ = fake_module(reply())
    modes = capture_mode_at_import(monkeypatch, lambda: module)

    LiteLLMChatModel("gpt-4o")

    assert modes == ["PRODUCTION"]
    assert "LITELLM_MODE" not in os.environ


def test_a_litellm_mode_chosen_by_the_caller_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LITELLM_MODE", "DEV")
    module, _ = fake_module(reply())
    modes = capture_mode_at_import(monkeypatch, lambda: module)

    LiteLLMChatModel("gpt-4o")

    assert modes == ["DEV"]
    assert os.environ["LITELLM_MODE"] == "DEV"


@pytest.mark.parametrize("failure", [ImportError("missing"), RuntimeError("broken")])
def test_the_litellm_mode_is_removed_again_when_the_import_fails(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    monkeypatch.delenv("LITELLM_MODE", raising=False)

    def fail() -> Any:
        raise failure

    capture_mode_at_import(monkeypatch, fail)

    with pytest.raises(ChatModelConfigurationError):
        LiteLLMChatModel("gpt-4o")

    assert "LITELLM_MODE" not in os.environ


def test_an_injected_module_is_used_without_importing_litellm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_import(monkeypatch, lambda: AssertionError("litellm must not be imported"))
    module, calls = fake_module(reply())

    run(build(module))

    assert len(calls) == 1


@pytest.mark.parametrize("module", [SimpleNamespace(), SimpleNamespace(completion=5)])
def test_a_module_without_a_completion_function_is_rejected(module: Any) -> None:
    with pytest.raises(ChatModelConfigurationError, match="completion"):
        build(module)


@pytest.mark.parametrize(
    "arguments",
    [
        {"model": ""},
        {"model": "   "},
        {"model": None},
        {"api_key": ""},
        {"api_key": "  "},
        {"api_base": ""},
        {"api_base": "  "},
    ],
)
def test_blank_constructor_arguments_are_rejected(arguments: dict[str, Any]) -> None:
    module, _ = fake_module(reply())
    arguments = {"model": "provider/model", **arguments}

    with pytest.raises(ChatModelConfigurationError):
        LiteLLMChatModel(module=module, **arguments)


@pytest.mark.parametrize(
    "arguments",
    [
        {"max_output_tokens": 0},
        {"max_output_tokens": True},
        {"max_output_tokens": "5"},
        {"timeout_seconds": 0},
        {"timeout_seconds": -1},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": True},
        {"json_output": "yes"},
    ],
)
def test_invalid_arguments_are_rejected_before_calling_litellm(
    arguments: dict[str, Any],
) -> None:
    module, calls = fake_module(reply())

    with pytest.raises(ChatModelError):
        run(build(module), **arguments)

    assert calls == []


@pytest.mark.parametrize(
    "messages",
    [(), [], "hello", b"hello", None, [{"role": "user", "content": "x"}]],
)
def test_invalid_messages_are_rejected_before_calling_litellm(messages: Any) -> None:
    module, calls = fake_module(reply())

    with pytest.raises(ChatModelError):
        build(module).complete(messages, max_output_tokens=16, timeout_seconds=5)

    assert calls == []


def test_repr_never_contains_the_key() -> None:
    module, _ = fake_module(reply())
    model = build(module, api_key=SECRET)

    assert repr(model) == "LiteLLMChatModel(model='provider/model')"
    assert SECRET not in f"{model!r} {model!s} {vars(model)!r}"


def test_importing_the_package_does_not_import_litellm() -> None:
    code = "import sys, dataindex_llm; sys.exit(int('litellm' in sys.modules))"

    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, timeout=120, check=False
    )

    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
