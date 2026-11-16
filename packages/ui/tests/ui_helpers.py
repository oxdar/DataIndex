"""Shared helpers for the dataindex-ui tests: a scripted model and app factory."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from dataindex import ChatModelError, DataIndexClient
from dataindex.llm import ChatMessage, ChatResult
from dataindex_ui.app import UISettings, create_app
from dataindex_ui.worker import ClientWorker

PORT = 8765
ORIGIN = f"http://127.0.0.1:{PORT}"
TOKEN = "test-session-token"
QUESTION = "What are the payment terms?"
CLAIM = "Invoices are due net 30 days after receipt."
DOCUMENT = (
    "# Supply agreement\n\n"
    "## Payment terms\n\n"
    "Invoices are due net 30 days after receipt. Late payments accrue 1% interest.\n\n"
    "## Delivery\n\n"
    "Goods ship within 10 business days.\n"
)

Policy = Callable[[dict[str, Any]], str]


def cite_payment_terms(payload: dict[str, Any]) -> str:
    """Search the documents once, then cite the first evidence returned."""
    untrusted = payload["untrusted_data"]
    if untrusted["evidence"]:
        return json.dumps(
            {
                "action": "answer",
                "claims": [
                    {"text": CLAIM, "evidence_ids": [untrusted["evidence"][0]["evidence_id"]]}
                ],
            }
        )
    tools = [tool for tool in untrusted["tools"] if tool["tool_id"] == "documents.search"]
    if payload["state"]["final_turn"] or not tools:
        return json.dumps({"action": "answer", "claims": []})
    return json.dumps(
        {
            "action": "call_tools",
            "calls": [{"ref": tools[0]["ref"], "parameters": {"query": "payment terms"}}],
        }
    )


class ScriptedModel:
    """An in-process chat model: no HTTP, no key, replies from a policy."""

    def __init__(
        self,
        policy: Policy = cite_payment_terms,
        *,
        delay: float = 0.0,
        fail: bool = False,
        gate: threading.Event | None = None,
    ) -> None:
        self.policy = policy
        self.delay = delay
        self.fail = fail
        self.gate = gate
        self.requests: list[tuple[ChatMessage, ...]] = []

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int,
        timeout_seconds: float,
        json_output: bool = True,
    ) -> ChatResult:
        self.requests.append(tuple(messages))
        if self.gate is not None:
            self.gate.wait(10)
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise ChatModelError("the scripted provider is unavailable")
        first_user = next(item for item in messages if item.role == "user")
        return ChatResult(self.policy(json.loads(first_user.content)), tokens_used=10)


def make_app(
    storage: Path,
    model: ScriptedModel | None = None,
    *,
    heartbeat_seconds: float = 2.0,
) -> tuple[Any, ClientWorker]:
    worker = ClientWorker(lambda: DataIndexClient(model, storage_path=storage))
    settings = UISettings(
        storage_path=storage,
        port=PORT,
        model_name=None if model is None else "scripted",
        token=TOKEN,
        heartbeat_seconds=heartbeat_seconds,
    )
    return create_app(settings, worker), worker


def signed_in(app: Any) -> TestClient:
    client = TestClient(app, base_url=ORIGIN)
    client.cookies.set(f"dataindex_ui_{PORT}", TOKEN)
    client.headers["Origin"] = ORIGIN
    return client


def read_events(response: Any) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for block in response.text.split("\n\n"):
        if not block.strip():
            continue
        name, data = "message", []
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        events.append((name, json.loads("\n".join(data))))
    return events
