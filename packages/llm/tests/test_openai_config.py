"""Configuration: base URL rules, key resolution and secret hygiene."""

from __future__ import annotations

import ssl
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from dataindex.exceptions import ChatModelConfigurationError
from llm_fakes import FakeServer, call, make_model

from dataindex_llm import OpenAICompatibleChatModel

KEY = "test-key-123"


def build(base: str, **kwargs: Any) -> OpenAICompatibleChatModel:
    kwargs.setdefault("api_key", KEY)
    kwargs.setdefault("environ", {})
    return OpenAICompatibleChatModel("m", api_base=base, **kwargs)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("https://api.openai.com/v1", "https://api.openai.com/v1"),
        ("https://api.openai.com/v1/", "https://api.openai.com/v1"),
        ("  https://openrouter.ai/api/v1  ", "https://openrouter.ai/api/v1"),
        ("https://gateway.example:8443/v1///", "https://gateway.example:8443/v1"),
        ("https://[2001:db8::1]/v1", "https://[2001:db8::1]/v1"),
        ("http://localhost:1234/v1", "http://localhost:1234/v1"),
        ("http://127.0.0.1:8000/v1/", "http://127.0.0.1:8000/v1"),
        ("http://[::1]:9/v1", "http://[::1]:9/v1"),
        ("HTTP://LOCALHOST:1234/v1/", "HTTP://LOCALHOST:1234/v1"),
        ("http://localhost", "http://localhost"),
    ],
)
def test_accepted_base_urls_are_normalised(given: str, expected: str) -> None:
    assert build(given).api_base == expected


@pytest.mark.parametrize(
    "base",
    [
        "http://localhost:1234/v1",
        "http://127.0.0.1:8000/v1",
        "http://[::1]:9/v1",
        "HTTP://LOCALHOST:1234/v1/",
    ],
)
def test_loopback_servers_need_no_key(base: str) -> None:
    model = OpenAICompatibleChatModel("m", api_base=base, environ={})

    assert model.api_base == base.rstrip("/")


@pytest.mark.parametrize(
    "base",
    [
        "http://example.com/v1",
        "http://127.0.0.2:8000/v1",
        "http://0.0.0.0:8000/v1",
        "http://localhost.example.com/v1",
        "ftp://example.com/v1",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "example.com/v1",
        "//example.com/v1",
        "https://",
        "https:///v1",
        "https://user:secret@example.com/v1",
        "https://secret@example.com/v1",
        "https://example.com/v1?key=secret",
        "https://example.com/v1?",
        "https://example.com/v1#secret",
        "https://example.com/v1#",
        "https://example.com/v1 secret",
        "https://example.com/v1\r\nX-Secret: 1",
        "https://exämple.com/v1",
        "https://example.com:99999/v1",
        "https://example.com:secret/v1",
        "https://[::1/v1",
    ],
)
def test_rejected_base_urls_never_echo_their_text(base: str) -> None:
    with pytest.raises(ChatModelConfigurationError) as info:
        build(base)

    text = str(info.value)
    assert "secret" not in text
    assert "example.com" not in text
    assert "api_base" in text


def test_a_rejected_environment_base_url_names_its_variable() -> None:
    environ = {"OPENAI_API_KEY": KEY, "OPENAI_BASE_URL": "http://example.com/v1"}

    with pytest.raises(ChatModelConfigurationError, match="OPENAI_BASE_URL"):
        OpenAICompatibleChatModel("m", environ=environ)


@pytest.mark.parametrize("environ", [{}, {"OPENAI_API_KEY": "   "}])
def test_a_key_is_required_for_remote_hosts(environ: dict[str, str]) -> None:
    with pytest.raises(ChatModelConfigurationError) as info:
        OpenAICompatibleChatModel(
            "m", api_base="https://example.com/v1", environ=environ
        )

    assert "OPENAI_API_KEY" in str(info.value)
    assert "chat_backend" in str(info.value)


def test_the_default_endpoint_is_openai_and_requires_a_key() -> None:
    with pytest.raises(ChatModelConfigurationError, match="OPENAI_API_KEY"):
        OpenAICompatibleChatModel("m", environ={})

    model = OpenAICompatibleChatModel("m", environ={"OPENAI_API_KEY": KEY})

    assert model.api_base == "https://api.openai.com/v1"


def test_environment_variables_are_resolved_from_the_injected_mapping() -> None:
    model = OpenAICompatibleChatModel(
        "m",
        environ={
            "OPENAI_API_KEY": f" {KEY} ",
            "OPENAI_BASE_URL": " https://gateway.example/v1/ ",
        },
    )

    assert model.api_base == "https://gateway.example/v1"


def test_blank_environment_values_count_as_unset() -> None:
    model = OpenAICompatibleChatModel(
        "m", environ={"OPENAI_API_KEY": KEY, "OPENAI_BASE_URL": "  "}
    )

    assert model.api_base == "https://api.openai.com/v1"


def test_the_process_environment_is_the_default(
    monkeypatch: pytest.MonkeyPatch, server: FakeServer
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setenv("OPENAI_BASE_URL", server.api_base)

    call(OpenAICompatibleChatModel("m"))

    (request,) = server.requests
    assert request.path == "/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"


def test_explicit_arguments_take_precedence_over_the_environment(
    server: FakeServer,
) -> None:
    other = FakeServer()
    other.start()
    try:
        environ = {"OPENAI_API_KEY": "from-env", "OPENAI_BASE_URL": other.api_base}
        call(make_model(server, api_key="explicit", environ=environ))
    finally:
        other.close()

    assert other.requests == []
    assert server.requests[0].headers["authorization"] == "Bearer explicit"


def test_an_environment_key_is_sent_to_the_configured_loopback_server(
    server: FakeServer,
) -> None:
    call(make_model(server, environ={"OPENAI_API_KEY": KEY}))

    assert server.requests[0].headers["authorization"] == f"Bearer {KEY}"


def test_surrounding_whitespace_is_removed_from_a_key(server: FakeServer) -> None:
    call(make_model(server, api_key=f"  {KEY}\n"))

    assert server.requests[0].headers["authorization"] == f"Bearer {KEY}"


def test_dotenv_files_are_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=file-key\nOPENAI_BASE_URL=http://127.0.0.1:1/v1\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ChatModelConfigurationError, match="OPENAI_API_KEY"):
        OpenAICompatibleChatModel("m")
    with pytest.raises(ChatModelConfigurationError, match="OPENAI_API_KEY"):
        OpenAICompatibleChatModel("m", environ={})


@pytest.mark.parametrize("name", ["", "   ", None, 5, b"model"])
def test_blank_or_invalid_model_names_are_rejected(name: Any) -> None:
    with pytest.raises(ChatModelConfigurationError, match="model name"):
        OpenAICompatibleChatModel(name, api_key=KEY, environ={})


def test_model_names_are_trimmed() -> None:
    assert OpenAICompatibleChatModel("  gpt-4o ", api_key=KEY, environ={}).model == (
        "gpt-4o"
    )


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("api_key", ""),
        ("api_key", "   "),
        ("api_key", 5),
        ("api_base", ""),
        ("api_base", "  "),
        ("api_base", b"https://example.com"),
    ],
)
def test_blank_explicit_arguments_are_rejected_not_ignored(
    argument: str, value: Any
) -> None:
    arguments: dict[str, Any] = {"api_key": KEY, "api_base": "https://example.com/v1"}
    arguments[argument] = value

    with pytest.raises(ChatModelConfigurationError, match=argument):
        OpenAICompatibleChatModel("m", environ={}, **arguments)


@pytest.mark.parametrize(
    "key",
    ["two words", "tab\there", "line\r\nbreak", "clé-123", "nul\x00byte"],
)
def test_keys_that_could_inject_headers_are_rejected(key: str) -> None:
    with pytest.raises(ChatModelConfigurationError) as info:
        build("https://example.com/v1", api_key=key)

    assert key not in str(info.value)

    with pytest.raises(ChatModelConfigurationError):
        OpenAICompatibleChatModel(
            "m", api_base="https://example.com/v1", environ={"OPENAI_API_KEY": key}
        )


@pytest.mark.parametrize("limit", [0, -1, True, "5", 1.5, None])
def test_the_response_limit_must_be_a_positive_integer(limit: Any) -> None:
    with pytest.raises(ChatModelConfigurationError, match="max_response_bytes"):
        build("https://example.com/v1", max_response_bytes=limit)


def test_repr_str_and_attributes_never_contain_the_key() -> None:
    secret = "sk-very-secret-value"
    model = build("https://gateway.example/v1", api_key=secret)

    assert repr(model) == (
        "OpenAICompatibleChatModel(model='m', api_base='https://gateway.example/v1')"
    )
    assert str(model) == repr(model)
    assert secret not in repr(vars(model))
    assert secret not in f"{model!r} {model!s} {model.model} {model.api_base}"
    assert repr(model._key) == "<redacted>"


def test_https_endpoints_verify_certificates() -> None:
    opener = build("https://gateway.example/v1")._get_opener()

    (handler,) = [
        item
        for item in opener.handlers
        if isinstance(item, urllib.request.HTTPSHandler)
    ]
    context = handler._context  # type: ignore[attr-defined]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
