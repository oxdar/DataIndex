"""Error mapping, secret hygiene and response parsing."""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import time
import urllib.error
from typing import Any

import pytest
from dataindex.exceptions import ChatModelError
from llm_fakes import (
    DEFAULT_TEXT,
    FakeServer,
    Reply,
    call,
    completion,
    error_document,
    make_model,
)

from dataindex_llm import OpenAICompatibleChatModel

KEY = "test-key-123"
FORBIDDEN = (KEY, "What is 2+2?", "Answer in JSON.", '"messages"', "Bearer")


def assert_clean(text: str) -> None:
    for fragment in FORBIDDEN:
        assert fragment not in text


class RaisingOpener:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def open(self, request: Any, timeout: float | None = None) -> Any:
        raise self.error


def failing_model(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> OpenAICompatibleChatModel:
    model = OpenAICompatibleChatModel(
        "m", api_base="http://127.0.0.1:9/v1", api_key=KEY, environ={}
    )
    monkeypatch.setattr(model, "_get_opener", lambda: RaisingOpener(error))
    return model


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "authentication or permission failed"),
        (403, "authentication or permission failed"),
        (404, "not found"),
        (418, "request rejected"),
        (429, "rate limited"),
        (500, "server error"),
        (503, "server error"),
    ],
)
def test_http_errors_report_status_reason_and_provider_message(
    server: FakeServer, status: int, reason: str
) -> None:
    server.script(Reply(status, error_document("upstream said no")))

    with pytest.raises(ChatModelError) as info:
        call(make_model(server, api_key=KEY))

    text = str(info.value)
    assert text == (
        f"the chat endpoint returned HTTP {status} ({reason}): upstream said no"
    )
    assert info.value.status_code == status
    assert_clean(text)


@pytest.mark.parametrize("status", [400, 500, 502])
def test_unstructured_error_bodies_are_never_echoed(
    server: FakeServer, status: int
) -> None:
    body = b"<html>/srv/internal " + KEY.encode() + b" Bearer abc</html>"
    server.script(Reply(status, raw=body))

    with pytest.raises(ChatModelError) as info:
        call(make_model(server, api_key=KEY))

    text = str(info.value)
    assert text.startswith(f"the chat endpoint returned HTTP {status} (")
    assert "<html>" not in text
    assert "/srv/internal" not in text
    assert_clean(text)


@pytest.mark.parametrize(
    "document",
    [
        {"error": "plain string error"},
        {"message": "top level message"},
    ],
)
def test_alternative_error_shapes_are_understood(
    server: FakeServer, document: dict[str, Any]
) -> None:
    server.script(Reply(500, document))

    with pytest.raises(ChatModelError) as info:
        call(make_model(server))

    assert str(info.value).endswith(next(iter(document.values())))


def test_provider_messages_are_redacted(server: FakeServer) -> None:
    server.script(
        Reply(
            401,
            error_document(
                f"Incorrect API key provided: {KEY}. Sent Authorization: "
                f"Bearer {KEY} and Bearer abcdefghijklmnop1234 as well."
            ),
        )
    )

    with pytest.raises(ChatModelError) as info:
        call(make_model(server, api_key=KEY))

    text = str(info.value)
    assert "[redacted]" in text
    assert KEY not in text
    assert "abcdefghijklmnop1234" not in text


def test_provider_messages_are_flattened_and_truncated(server: FakeServer) -> None:
    message = "line one\nline two\x1b[31m\x00 " + "x" * 5000
    server.script(Reply(500, error_document(message)))

    with pytest.raises(ChatModelError) as info:
        call(make_model(server))

    text = str(info.value)
    assert "\n" not in text and "\x1b" not in text and "\x00" not in text
    assert text.endswith("...")
    assert len(text) < 300


def test_huge_error_bodies_are_tolerated(server: FakeServer) -> None:
    server.script(Reply(500, raw=b"x" * 300_000))

    with pytest.raises(ChatModelError, match="HTTP 500"):
        call(make_model(server))


def test_truncated_error_bodies_are_tolerated(server: FakeServer) -> None:
    server.script(Reply(500, raw=b'{"error": {"mess', truncate=True))

    with pytest.raises(ChatModelError, match="HTTP 500"):
        call(make_model(server))


def test_server_closing_without_a_reply_is_reported_by_class_name(
    server: FakeServer,
) -> None:
    server.script(Reply(close_without_reply=True))

    with pytest.raises(ChatModelError) as info:
        call(make_model(server, api_key=KEY))

    assert str(info.value) == (
        "could not reach the chat endpoint (RemoteDisconnected)"
    )


def test_a_cut_off_success_body_is_an_error(server: FakeServer) -> None:
    server.script(Reply(200, raw=b'{"choices": [{"mess', truncate=True))

    with pytest.raises(ChatModelError):
        call(make_model(server))


def test_a_refused_connection_is_reported_by_class_name() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    model = OpenAICompatibleChatModel(
        "m", api_base=f"http://127.0.0.1:{port}/v1", environ={}
    )

    with pytest.raises(ChatModelError) as info:
        call(model, timeout_seconds=10)

    assert str(info.value) == (
        "could not reach the chat endpoint (ConnectionRefusedError)"
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            urllib.error.URLError(
                ConnectionRefusedError(10061, "refused http://127.0.0.1:1/secret/path")
            ),
            "could not reach the chat endpoint (ConnectionRefusedError)",
        ),
        (
            urllib.error.URLError("unknown url type: secret://host"),
            "could not reach the chat endpoint (URLError)",
        ),
        (
            OSError("connect failed for https://host.example/path?key=secret"),
            "could not reach the chat endpoint (OSError)",
        ),
        (
            ssl.SSLCertVerificationError("certificate verify failed: secret.example"),
            "could not reach the chat endpoint (SSLCertVerificationError)",
        ),
        (
            socket.gaierror(11001, "getaddrinfo failed for secret.example"),
            "could not reach the chat endpoint (gaierror)",
        ),
        (
            http.client.BadStatusLine("secret"),
            "could not reach the chat endpoint (BadStatusLine)",
        ),
        (ValueError("secret"), "the chat request failed (ValueError)"),
    ],
)
def test_transport_failures_never_expose_exception_text(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, expected: str
) -> None:
    with pytest.raises(ChatModelError) as info:
        call(failing_model(monkeypatch, error))

    assert str(info.value) == expected
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("secret"),
        socket.timeout("secret"),
        urllib.error.URLError(TimeoutError("secret")),
    ],
)
def test_socket_timeouts_are_reported_as_timed_out(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    with pytest.raises(ChatModelError, match="timed out") as info:
        call(failing_model(monkeypatch, error))

    assert "secret" not in str(info.value)


def test_an_expired_deadline_fails_before_connecting(server: FakeServer) -> None:
    model = make_model(server)

    with pytest.raises(ChatModelError, match="timed out"):
        model._exchange(b"{}", time.monotonic() - 1)

    assert server.requests == []


def test_responses_larger_than_the_limit_are_rejected(server: FakeServer) -> None:
    server.script(Reply(200, completion("x" * 5000)))

    with pytest.raises(ChatModelError, match="too large"):
        call(make_model(server, max_response_bytes=1024))


def test_response_size_limit_is_inclusive(server: FakeServer) -> None:
    body = json.dumps(completion("x" * 10)).encode("utf-8")
    server.script(Reply(200, raw=body), Reply(200, raw=body))

    assert call(make_model(server, max_response_bytes=len(body))).text == "x" * 10
    with pytest.raises(ChatModelError, match="too large"):
        call(make_model(server, max_response_bytes=len(body) - 1))


def test_streamed_responses_stop_at_the_limit(server: FakeServer) -> None:
    server.script(Reply(200, chunks=(b"x" * 1000,) * 50))

    with pytest.raises(ChatModelError, match="too large"):
        call(make_model(server, max_response_bytes=2048))


@pytest.mark.parametrize(
    "document",
    [
        {"choices": []},
        {"choices": [{}]},
        {"choices": [None]},
        {"choices": "nope"},
        {"choices": [{"message": None}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"message": {"content": 5}}]},
        {"choices": [{"message": {"content": [{"type": "text", "text": "hi"}]}}]},
        {"usage": {"total_tokens": 3}},
        [],
        "text",
        None,
    ],
)
def test_missing_or_non_string_content_is_an_error(
    server: FakeServer, document: Any
) -> None:
    server.script(Reply(200, document))

    with pytest.raises(ChatModelError, match="returned no text content"):
        call(make_model(server))


def test_refusals_are_reported_without_echoing_them(server: FakeServer) -> None:
    refusal = "I cannot help with the confidential plan"
    server.script(
        Reply(200, {"choices": [{"message": {"content": None, "refusal": refusal}}]})
    )

    with pytest.raises(ChatModelError, match="refused") as info:
        call(make_model(server))

    assert "confidential" not in str(info.value)


def test_empty_text_is_a_valid_reply(server: FakeServer) -> None:
    server.script(Reply(200, completion("")))

    assert call(make_model(server)).text == ""


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ({"total_tokens": 42, "prompt_tokens": 1, "completion_tokens": 1}, 42),
        ({"total_tokens": 0}, 0),
        ({"prompt_tokens": 30, "completion_tokens": 12}, 42),
        ({"total_tokens": -1, "prompt_tokens": 3, "completion_tokens": 4}, 7),
        ({"total_tokens": "42", "prompt_tokens": 1, "completion_tokens": 2}, 3),
        ({"total_tokens": True}, None),
        ({"total_tokens": 1.5}, None),
        ({"prompt_tokens": 30}, None),
        ({"prompt_tokens": -1, "completion_tokens": 5}, None),
        ({"prompt_tokens": True, "completion_tokens": 5}, None),
        ({}, None),
        ("many", None),
        (None, None),
    ],
)
def test_token_usage_variants(
    server: FakeServer, usage: Any, expected: int | None
) -> None:
    server.script(Reply(200, completion(DEFAULT_TEXT, usage=usage)))

    assert call(make_model(server)).tokens_used == expected


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_json_constants_are_rejected(
    server: FakeServer, literal: str
) -> None:
    body = (
        '{"choices":[{"message":{"content":"ok"}}],"usage":{"total_tokens":%s}}'
        % literal
    )
    server.script(Reply(200, raw=body.encode("ascii")))

    with pytest.raises(ChatModelError, match="not valid JSON"):
        call(make_model(server))


@pytest.mark.parametrize(
    "raw",
    [b"\xff\xfe\x00", b"not json", b"", b"{", b"\xef\xbb\xbf{}", b"[" * 200_000],
)
def test_invalid_success_bodies_are_rejected(server: FakeServer, raw: bytes) -> None:
    server.script(Reply(200, raw=raw))

    with pytest.raises(ChatModelError, match="not valid JSON"):
        call(make_model(server))


def test_a_valid_reply_returns_text_and_usage(server: FakeServer) -> None:
    server.script(Reply(200, completion("hello", usage={"total_tokens": 9})))

    result = call(make_model(server))

    assert (result.text, result.tokens_used) == ("hello", 9)
