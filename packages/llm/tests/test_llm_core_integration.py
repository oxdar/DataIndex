"""End to end: dataindex-core resolves and drives this adapter."""

from __future__ import annotations

import sys
import time
from typing import Any

import pytest
from dataindex.exceptions import ChatModelConfigurationError, ChatModelError
from dataindex.llm import ModelPlanner, resolve_chat_model
from dataindex.reasoning import PlannerTurn
from llm_fakes import FakeServer, Reply, error_document, ok

from dataindex_llm import OpenAICompatibleChatModel

ANSWER = '{"action": "answer", "claims": []}'
QUESTION = "How many open orders are there?"


def plan(model: Any, **overrides: Any) -> PlannerTurn:
    arguments: dict[str, Any] = {
        "question": QUESTION,
        "tools": (),
        "evidence": (),
        "depth": 0,
        "remaining_tool_calls": 2,
        "remaining_tokens": 8_000,
        "deadline_monotonic": time.monotonic() + 10,
    }
    arguments.update(overrides)
    return ModelPlanner(model).plan(**arguments)


def resolve(server: FakeServer) -> Any:
    return resolve_chat_model("some-model", {"api_base": server.api_base})


def test_core_resolves_a_model_name_through_this_package(server: FakeServer) -> None:
    model = resolve(server)

    assert isinstance(model, OpenAICompatibleChatModel)
    assert model.model == "some-model"


def test_the_planner_completes_one_round_trip(server: FakeServer) -> None:
    server.script(ok(ANSWER, usage={"total_tokens": 42}))

    turn = plan(resolve(server))

    assert (turn.tool_calls, turn.claims, turn.tokens_used) == ((), (), 42)
    (request,) = server.requests
    body = request.json
    assert request.path == "/v1/chat/completions"
    assert body["model"] == "some-model"
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    assert QUESTION in body["messages"][1]["content"]
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == 1_024
    assert "authorization" not in request.headers


def test_tokens_are_estimated_when_the_provider_reports_no_usage(
    server: FakeServer,
) -> None:
    server.script(ok(ANSWER))

    assert plan(resolve(server)).tokens_used > 0


def test_a_malformed_reply_is_repaired_with_a_second_request(
    server: FakeServer,
) -> None:
    server.script(
        ok("not json", usage={"total_tokens": 10}),
        ok(ANSWER, usage={"total_tokens": 32}),
    )

    turn = plan(resolve(server))

    assert turn.tokens_used == 42
    roles = [
        [message["role"] for message in request.json["messages"]]
        for request in server.requests
    ]
    assert roles == [["system", "user"], ["system", "user", "assistant", "user"]]


def test_provider_errors_reach_the_caller_as_chat_model_errors(
    server: FakeServer,
) -> None:
    server.script(Reply(401, error_document("Incorrect API key provided.")))

    with pytest.raises(ChatModelError, match="HTTP 401") as info:
        plan(resolve(server))

    assert QUESTION not in str(info.value)


def test_the_planner_deadline_is_enforced_by_the_adapter(server: FakeServer) -> None:
    server.script(Reply(delay=5.0))
    started = time.monotonic()

    with pytest.raises(ChatModelError, match="timed out"):
        plan(resolve(server), deadline_monotonic=time.monotonic() + 0.3)

    assert time.monotonic() - started < 3


def test_a_missing_key_fails_when_the_model_is_resolved() -> None:
    with pytest.raises(ChatModelConfigurationError, match="OPENAI_API_KEY"):
        resolve_chat_model("some-model")


def test_a_missing_litellm_surfaces_as_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "litellm", None)

    with pytest.raises(ChatModelConfigurationError, match="litellm"):
        resolve_chat_model("litellm:some-model")
