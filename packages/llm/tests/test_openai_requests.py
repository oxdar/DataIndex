"""Request construction, parameter fallback, deadlines, redirects and proxies."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from dataindex.exceptions import ChatModelError
from dataindex.llm import ChatMessage
from llm_fakes import (
    DEFAULT_TEXT,
    MESSAGES,
    FakeServer,
    Reply,
    call,
    completion,
    error_document,
    make_model,
)

from dataindex_llm import OpenAICompatibleChatModel, __version__
from dataindex_llm.openai_compatible import _Parameters

KEY = "test-key-123"


def encoded(document: Any) -> bytes:
    return json.dumps(document).encode("utf-8")


def stub_exchange(
    monkeypatch: pytest.MonkeyPatch,
    model: OpenAICompatibleChatModel,
    replies: list[tuple[int, bytes]],
) -> tuple[list[dict[str, Any]], list[float]]:
    """Replace the transport seam; returns the request bodies and deadlines."""
    bodies: list[dict[str, Any]] = []
    deadlines: list[float] = []
    pending = iter(replies)

    def exchange(body: bytes, deadline: float) -> tuple[int, bytes]:
        bodies.append(json.loads(body))
        deadlines.append(deadline)
        return next(pending)

    monkeypatch.setattr(model, "_exchange", exchange)
    return bodies, deadlines


def test_request_shape_on_loopback_without_a_key(server: FakeServer) -> None:
    result = call(make_model(server))

    assert result.text == DEFAULT_TEXT
    (request,) = server.requests
    assert request.method == "POST"
    assert request.path == "/v1/chat/completions"
    assert request.json == {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "Answer in JSON."},
            {"role": "user", "content": "What is 2+2?"},
        ],
        "temperature": 0,
        "max_tokens": 256,
        "response_format": {"type": "json_object"},
    }
    assert request.headers["content-type"] == "application/json"
    assert request.headers["accept"] == "application/json"
    assert request.headers["user-agent"] == f"dataindex-llm/{__version__}"
    assert "authorization" not in request.headers


def test_authorization_header_is_sent_when_a_key_is_given(server: FakeServer) -> None:
    call(make_model(server, api_key=KEY))

    assert server.requests[0].headers["authorization"] == f"Bearer {KEY}"


def test_trailing_slash_in_the_base_url_is_normalised(server: FakeServer) -> None:
    model = OpenAICompatibleChatModel(
        "test-model", api_base=server.api_base + "/", environ={}
    )

    call(model)

    assert server.requests[0].path == "/v1/chat/completions"


def test_non_ascii_prompts_round_trip_unchanged(server: FakeServer) -> None:
    text = "héllo ✓ 日本語 😀 \ud800"
    model = make_model(server)

    model.complete(
        (ChatMessage("user", text),), max_output_tokens=16, timeout_seconds=5
    )

    assert server.requests[0].json["messages"][0]["content"] == text


def test_json_output_false_omits_response_format(server: FakeServer) -> None:
    call(make_model(server), json_output=False)

    body = server.requests[0].json
    assert "response_format" not in body
    assert body["temperature"] == 0


@pytest.mark.parametrize(
    ("api_base", "parameter"),
    [
        ("https://api.openai.com/v1", "max_completion_tokens"),
        ("https://API.OpenAI.com:443/v1", "max_completion_tokens"),
        ("https://openrouter.ai/api/v1", "max_tokens"),
        ("https://api.openai.com.example.net/v1", "max_tokens"),
    ],
)
def test_token_limit_parameter_depends_on_the_host(
    monkeypatch: pytest.MonkeyPatch, api_base: str, parameter: str
) -> None:
    model = OpenAICompatibleChatModel(
        "gpt-4o", api_base=api_base, api_key=KEY, environ={}
    )
    bodies, _ = stub_exchange(monkeypatch, model, [(200, encoded(completion()))])

    call(model)

    other = {"max_tokens", "max_completion_tokens"} - {parameter}
    assert bodies[0][parameter] == 256
    assert other.isdisjoint(bodies[0])


@pytest.mark.parametrize(
    "arguments",
    [
        {"max_output_tokens": 0},
        {"max_output_tokens": -5},
        {"max_output_tokens": True},
        {"max_output_tokens": 1.5},
        {"max_output_tokens": "10"},
        {"timeout_seconds": 0},
        {"timeout_seconds": -1.0},
        {"timeout_seconds": True},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": "5"},
        {"timeout_seconds": None},
        {"json_output": "yes"},
        {"json_output": 1},
    ],
)
def test_invalid_arguments_fail_before_any_request(
    server: FakeServer, arguments: dict[str, Any]
) -> None:
    with pytest.raises(ChatModelError):
        call(make_model(server), **arguments)

    assert server.requests == []


@pytest.mark.parametrize(
    "messages",
    [
        (),
        [],
        "hello",
        None,
        [{"role": "user", "content": "x"}],
        [ChatMessage("user", "x"), 1],
    ],
)
def test_invalid_messages_fail_before_any_request(
    server: FakeServer, messages: Any
) -> None:
    with pytest.raises(ChatModelError):
        make_model(server).complete(messages, max_output_tokens=8, timeout_seconds=5)

    assert server.requests == []


def test_any_sequence_of_messages_is_accepted(server: FakeServer) -> None:
    model = make_model(server)

    model.complete(list(MESSAGES), max_output_tokens=8, timeout_seconds=5)

    assert len(server.requests[0].json["messages"]) == 2


def test_rejected_response_format_is_retried_without_only_that_parameter(
    server: FakeServer,
) -> None:
    server.script(
        Reply(
            400,
            error_document(
                "Invalid parameter: 'response_format' of type 'json_object' is "
                "not supported with this model."
            ),
        )
    )

    assert call(make_model(server)).text == DEFAULT_TEXT

    first, second = (request.json for request in server.requests)
    assert first["response_format"] == {"type": "json_object"}
    assert "response_format" not in second
    assert second["temperature"] == 0
    assert second["max_tokens"] == 256


def test_rejected_max_tokens_is_swapped_for_max_completion_tokens(
    server: FakeServer,
) -> None:
    server.script(
        Reply(
            400,
            error_document(
                "Unsupported parameter: 'max_tokens' is not supported with this "
                "model. Use 'max_completion_tokens' instead."
            ),
        )
    )

    call(make_model(server))

    first, second = (request.json for request in server.requests)
    assert first["max_tokens"] == 256
    assert "max_completion_tokens" not in first
    assert second["max_completion_tokens"] == 256
    assert "max_tokens" not in second
    assert second["response_format"] == {"type": "json_object"}
    assert second["temperature"] == 0


def test_rejected_max_completion_tokens_is_swapped_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = OpenAICompatibleChatModel("gpt-4o", api_key=KEY, environ={})
    bodies, _ = stub_exchange(
        monkeypatch,
        model,
        [
            (400, encoded(error_document("Unknown parameter: max_completion_tokens"))),
            (200, encoded(completion())),
        ],
    )

    call(model)

    assert bodies[0]["max_completion_tokens"] == 256
    assert "max_completion_tokens" not in bodies[1]
    assert bodies[1]["max_tokens"] == 256


def test_rejected_temperature_is_dropped_on_http_422(server: FakeServer) -> None:
    server.script(
        Reply(422, error_document("temperature is not supported with this model"))
    )

    call(make_model(server))

    first, second = (request.json for request in server.requests)
    assert first["temperature"] == 0
    assert "temperature" not in second
    assert second["response_format"] == {"type": "json_object"}
    assert second["max_tokens"] == 256


def test_parameter_named_only_in_the_error_param_field_is_adjusted(
    server: FakeServer,
) -> None:
    server.script(Reply(400, error_document("Invalid value.", param="temperature")))

    call(make_model(server))

    assert "temperature" not in server.requests[1].json


def test_every_parameter_the_error_mentions_is_adjusted_at_once(
    server: FakeServer,
) -> None:
    server.script(
        Reply(400, error_document("response_format and temperature are unsupported"))
    )

    call(make_model(server))

    second = server.requests[1].json
    assert "response_format" not in second
    assert "temperature" not in second
    assert second["max_tokens"] == 256


def test_raw_error_text_is_searched_when_the_body_is_not_structured(
    server: FakeServer,
) -> None:
    server.script(Reply(400, raw=b"Unsupported parameter: Response_Format"))

    call(make_model(server))

    assert "response_format" not in server.requests[1].json


def test_unrelated_client_errors_are_not_retried(server: FakeServer) -> None:
    server.script(Reply(400, error_document("the messages array is too long")))

    with pytest.raises(ChatModelError, match="HTTP 400"):
        call(make_model(server))

    assert len(server.requests) == 1


def test_a_parameter_that_was_never_sent_is_not_retried(server: FakeServer) -> None:
    server.script(Reply(400, error_document("response_format is not supported")))

    with pytest.raises(ChatModelError, match="HTTP 400"):
        call(make_model(server), json_output=False)

    assert len(server.requests) == 1


@pytest.mark.parametrize("status", [401, 403, 404, 413, 429, 500, 503])
def test_other_statuses_fail_immediately_even_if_they_mention_parameters(
    server: FakeServer, status: int
) -> None:
    server.script(
        Reply(status, error_document("response_format max_tokens temperature"))
    )

    with pytest.raises(ChatModelError, match=f"HTTP {status}"):
        call(make_model(server))

    assert len(server.requests) == 1


def test_each_parameter_is_adjusted_once_and_the_fourth_error_is_final(
    server: FakeServer,
) -> None:
    server.script(
        Reply(400, error_document("response_format is not supported")),
        Reply(400, error_document("temperature is not supported")),
        Reply(400, error_document("max_tokens is not supported")),
        Reply(400, error_document("response_format is still not supported")),
        Reply(400, error_document("must never be requested")),
    )

    with pytest.raises(ChatModelError, match="HTTP 400"):
        call(make_model(server))

    bodies = [request.json for request in server.requests]
    assert len(bodies) == 4
    assert "response_format" not in bodies[1]
    assert bodies[1]["temperature"] == 0
    assert "temperature" not in bodies[2]
    assert bodies[2]["max_tokens"] == 256
    assert bodies[3]["max_completion_tokens"] == 256
    assert "max_tokens" not in bodies[3]


def test_retry_cap_holds_even_if_adjustments_keep_succeeding(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_Parameters, "adjust", lambda self, text: True)
    server.fallback = Reply(400, error_document("response_format"))

    with pytest.raises(ChatModelError, match="HTTP 400"):
        call(make_model(server))

    assert len(server.requests) == 4


def test_retries_reuse_the_original_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    model = OpenAICompatibleChatModel(
        "m", api_base="http://127.0.0.1:9/v1", environ={}
    )
    _, deadlines = stub_exchange(
        monkeypatch,
        model,
        [
            (400, encoded(error_document("response_format"))),
            (400, encoded(error_document("temperature"))),
            (200, encoded(completion())),
        ],
    )

    call(model)

    assert len(deadlines) == 3
    assert len(set(deadlines)) == 1


def test_slow_retries_share_one_overall_deadline(server: FakeServer) -> None:
    server.script(
        Reply(400, error_document("response_format"), delay=0.2),
        Reply(400, error_document("temperature"), delay=0.2),
        Reply(400, error_document("max_tokens"), delay=0.2),
        Reply(400, error_document("response_format"), delay=0.2),
    )
    started = time.monotonic()

    with pytest.raises(ChatModelError, match="timed out"):
        call(make_model(server), timeout_seconds=0.5)

    assert time.monotonic() - started < 1.5
    assert len(server.requests) <= 3


def test_slow_server_times_out_quickly(server: FakeServer) -> None:
    server.script(Reply(200, completion(), delay=3.0))
    started = time.monotonic()

    with pytest.raises(ChatModelError, match="timed out"):
        call(make_model(server), timeout_seconds=0.3)

    assert time.monotonic() - started < 2.0


def test_deadline_covers_a_body_that_drips_slowly(server: FakeServer) -> None:
    server.script(Reply(200, chunks=(b" ",) * 40, chunk_gap=0.15))
    started = time.monotonic()

    with pytest.raises(ChatModelError, match="timed out"):
        call(make_model(server), timeout_seconds=0.6)

    assert time.monotonic() - started < 3.0


@pytest.mark.parametrize("status", [302, 307, 308])
@pytest.mark.parametrize("kind", ["absolute", "relative", "file"])
def test_redirects_are_never_followed(
    server_factory: Any, status: int, kind: str
) -> None:
    target: FakeServer = server_factory()
    origin: FakeServer = server_factory()
    location = {
        "absolute": f"{target.url}/v1/chat/completions",
        "relative": "/elsewhere",
        "file": "file:///etc/passwd",
    }[kind]
    origin.script(Reply(status, raw=b"", headers=(("Location", location),)))

    with pytest.raises(ChatModelError, match="redirect") as info:
        call(make_model(origin, api_key=KEY))

    assert target.requests == []
    assert len(origin.requests) == 1
    assert KEY not in str(info.value)
    assert info.value.status_code == status


def test_loopback_requests_ignore_environment_proxies(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")

    call(make_model(server))

    assert len(server.requests) == 1


def test_remote_hosts_use_environment_proxies_without_leaking_the_key(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fake server plays the proxy: it records the CONNECT and refuses it.
    server.script(Reply(403, raw=b""))
    monkeypatch.setenv("HTTPS_PROXY", server.url)
    monkeypatch.setenv("https_proxy", server.url)
    model = OpenAICompatibleChatModel(
        "m", api_base="https://llm.example.invalid/v1", api_key=KEY, environ={}
    )

    with pytest.raises(ChatModelError, match="could not reach the chat endpoint"):
        call(model)

    (request,) = server.requests
    assert request.method == "CONNECT"
    assert request.path == "llm.example.invalid:443"
    assert "authorization" not in request.headers
    assert KEY not in json.dumps(request.headers)


def test_success_with_any_2xx_status_is_accepted(server: FakeServer) -> None:
    server.script(Reply(201, completion("created")))

    assert call(make_model(server)).text == "created"
