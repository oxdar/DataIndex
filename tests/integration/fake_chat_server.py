"""A loopback OpenAI-compatible chat server for end-to-end tests.

Replies come from a Python callable, so a test can play a well-behaved or a
malicious planner without a network connection or an API key.
"""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

Responder = Callable[[Mapping[str, Any]], str]
# A planner policy sees the parsed planner payload and returns the raw reply text.
PlannerPolicy = Callable[[dict[str, Any]], str]


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    owner: FakeChatServer


class HttpFailure(Exception):
    """Raised by a responder to make the server answer with an HTTP error status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        owner = cast(_Server, self.server).owner
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        owner.paths.append(self.path)
        owner.headers.append({name.lower(): value for name, value in self.headers.items()})
        owner.requests.append(body)
        try:
            content = owner.responder(body)
            status = 200
            document: Any = {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"total_tokens": 10},
            }
        except HttpFailure as failure:
            status = failure.status
            document = {"error": {"message": failure.message}}
        except Exception as error:
            owner.errors.append(error)
            status = 500
            document = {"error": {"message": "the scripted responder failed"}}
        payload = json.dumps(document).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True


class FakeChatServer:
    """Serves ``/v1/chat/completions`` on a free loopback port."""

    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self.requests: list[Mapping[str, Any]] = []
        self.paths: list[str] = []
        self.headers: list[dict[str, str]] = []
        self.errors: list[Exception] = []
        self._httpd = _Server(("127.0.0.1", 0), _Handler)
        self._httpd.owner = self
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        self._thread.start()

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> FakeChatServer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def planner_payload(body: Mapping[str, Any]) -> dict[str, Any]:
    """The planner's JSON user message: question, state and untrusted data.

    A repair request appends the rejected reply and an error message after the
    payload, so the payload is the first user message, not the last message.
    """
    first_user = next(item for item in body["messages"] if item["role"] == "user")
    return cast(dict[str, Any], json.loads(first_user["content"]))


def system_prompt(body: Mapping[str, Any]) -> str:
    """The system message of a planner request."""
    return cast(
        str, next(item["content"] for item in body["messages"] if item["role"] == "system")
    )


def is_repair(body: Mapping[str, Any]) -> bool:
    """Whether this request asks the model to fix a reply that was rejected."""
    last = body["messages"][-1]
    return last["role"] == "user" and last["content"].startswith(
        "Your reply was rejected"
    )


def planner_responder(policy: PlannerPolicy) -> Responder:
    """Serve the replies of a policy that reads the parsed planner payload."""

    def respond(body: Mapping[str, Any]) -> str:
        return policy(planner_payload(body))

    return respond


def call_reply(ref: str, parameters: Mapping[str, Any]) -> str:
    """A planner reply that calls one tool."""
    return json.dumps(
        {"action": "call_tools", "calls": [{"ref": ref, "parameters": dict(parameters)}]}
    )


def answer_reply(*claims: tuple[str, Sequence[str]]) -> str:
    """A planner reply that answers with ``(text, evidence_ids)`` claims."""
    return json.dumps(
        {
            "action": "answer",
            "claims": [
                {"text": text, "evidence_ids": list(evidence_ids)}
                for text, evidence_ids in claims
            ],
        }
    )


def tool_ref(payload: Mapping[str, Any], tool_id: str) -> str:
    """The planner ref of the first offered tool with this ``tool_id``."""
    return cast(
        str,
        next(
            tool["ref"]
            for tool in payload["untrusted_data"]["tools"]
            if tool["tool_id"] == tool_id
        ),
    )


def scripted(*steps: str | PlannerPolicy) -> PlannerPolicy:
    """Play one step per request, repeating the last step.

    A step is a fixed reply or a policy. A repair request counts as a request, so
    a script can make a model misbehave once and recover, or misbehave for good.
    """
    remaining = list(steps)

    def policy(payload: dict[str, Any]) -> str:
        step = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return step if isinstance(step, str) else step(payload)

    return policy


def calls_then_cites_policy(
    tool_id: str, parameters: Mapping[str, Any], claim: str
) -> PlannerPolicy:
    """Call ``tool_id`` once, then cite the first evidence it returned."""

    def policy(payload: dict[str, Any]) -> str:
        untrusted = payload["untrusted_data"]
        evidence = untrusted["evidence"]
        if evidence:
            return answer_reply((claim, [evidence[0]["evidence_id"]]))
        tools = [tool for tool in untrusted["tools"] if tool["tool_id"] == tool_id]
        if payload["state"]["final_turn"] or not tools:
            return answer_reply()
        return call_reply(tools[0]["ref"], parameters)

    return policy


def calls_then_cites(
    tool_id: str, parameters: Mapping[str, Any], claim: str
) -> Responder:
    """Call ``tool_id`` once, then cite the first evidence it returned."""
    return planner_responder(calls_then_cites_policy(tool_id, parameters, claim))
