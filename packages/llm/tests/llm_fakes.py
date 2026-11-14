"""A scripted loopback HTTP server for adapter tests; no network, no real keys."""

from __future__ import annotations

import http.server
import json
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, cast

from dataindex.llm import ChatMessage, ChatResult

from dataindex_llm import OpenAICompatibleChatModel

DEFAULT_TEXT = '{"action": "answer", "claims": []}'
MESSAGES = (
    ChatMessage("system", "Answer in JSON."),
    ChatMessage("user", "What is 2+2?"),
)


def make_model(server: FakeServer, **kwargs: Any) -> OpenAICompatibleChatModel:
    """A model aimed at ``server`` with an empty, injected environment."""
    kwargs.setdefault("environ", {})
    return OpenAICompatibleChatModel("test-model", api_base=server.api_base, **kwargs)


def call(model: Any, **kwargs: Any) -> ChatResult:
    kwargs.setdefault("max_output_tokens", 256)
    kwargs.setdefault("timeout_seconds", 5.0)
    return model.complete(MESSAGES, **kwargs)


def completion(content: Any = DEFAULT_TEXT, *, usage: Any = None) -> dict[str, Any]:
    """An OpenAI-style chat completion document; ``usage`` is omitted if None."""
    document: dict[str, Any] = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }
    if usage is not None:
        document["usage"] = usage
    return document


def error_document(message: str, **fields: Any) -> dict[str, Any]:
    return {"error": {"message": message, "type": "invalid_request_error", **fields}}


@dataclass(frozen=True)
class Reply:
    """One scripted response; ``body`` is JSON-encoded unless ``raw`` is given."""

    status: int = 200
    body: Any = None
    raw: bytes | None = None
    headers: tuple[tuple[str, str], ...] = ()
    delay: float = 0.0
    chunks: tuple[bytes, ...] | None = None
    chunk_gap: float = 0.0
    close_without_reply: bool = False
    truncate: bool = False


def ok(content: Any = DEFAULT_TEXT, *, usage: Any = None) -> Reply:
    return Reply(200, completion(content, usage=usage))


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    @property
    def json(self) -> Any:
        return json.loads(self.body)


class _Httpd(http.server.ThreadingHTTPServer):
    daemon_threads = True
    owner: FakeServer

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Clients that give up (timeouts, redirects) make handlers fail noisily.
        pass


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _serve(self) -> None:
        owner = cast(_Httpd, self.server).owner
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        headers = {name.lower(): value for name, value in self.headers.items()}
        reply = owner.next_reply(
            RecordedRequest(self.command, self.path, headers, body)
        )
        self.close_connection = True
        try:
            _write_reply(self, reply, owner.stop)
        except OSError:
            pass

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _serve
    do_HEAD = do_OPTIONS = do_CONNECT = _serve


def _write_reply(
    handler: http.server.BaseHTTPRequestHandler, reply: Reply, stop: threading.Event
) -> None:
    if reply.delay and stop.wait(reply.delay):
        return
    if reply.close_without_reply:
        return
    handler.send_response(reply.status)
    for name, value in reply.headers:
        handler.send_header(name, value)
    if "content-type" not in {name.lower() for name, _ in reply.headers}:
        handler.send_header("Content-Type", "application/json")
    handler.send_header("Connection", "close")
    if reply.chunks is not None:
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        for chunk in reply.chunks:
            handler.wfile.write(f"{len(chunk):x}\r\n".encode("ascii") + chunk + b"\r\n")
            if stop.wait(reply.chunk_gap):
                return
        handler.wfile.write(b"0\r\n\r\n")
        return
    payload = (
        reply.raw if reply.raw is not None else json.dumps(reply.body).encode("utf-8")
    )
    declared = len(payload) + (10 if reply.truncate else 0)
    handler.send_header("Content-Length", str(declared))
    handler.end_headers()
    handler.wfile.write(payload)


class FakeServer:
    """Records every request and answers from a script, then from ``fallback``."""

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self.fallback = ok()
        self.stop = threading.Event()
        self._script: deque[Reply] = deque()
        self._lock = threading.Lock()
        self._httpd = _Httpd(("127.0.0.1", 0), _Handler)
        self._httpd.owner = self
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def api_base(self) -> str:
        return f"{self.url}/v1"

    def script(self, *replies: Reply) -> None:
        with self._lock:
            self._script.extend(replies)

    def next_reply(self, request: RecordedRequest) -> Reply:
        with self._lock:
            self.requests.append(request)
            return self._script.popleft() if self._script else self.fallback

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self.stop.set()
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
