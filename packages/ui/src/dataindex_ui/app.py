"""The local web chat: a FastAPI app with one static page and SSE answers.

The server is meant for one user on one computer. It only answers requests whose
``Host`` header names the loopback address and port it listens on, which defeats
DNS rebinding. The browser proves it opened the start link with a random
session token, kept in an HttpOnly, SameSite=Strict cookie. Requests that
change data must also come from the page's own origin. All retrieval runs
through ``DataIndexClient.chat``, so the central dispatcher still checks
source authorization and entitlement before every tool call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from dataindex import DataIndexClient, DataIndexError, DocumentNotFoundError
from dataindex.local import (
    DOCUMENT_SUFFIXES,
    MAX_SUBMITTED_BINARY_FILE_BYTES,
    MAX_SUBMITTED_FILE_BYTES,
    submitted_file_limit,
)

from dataindex_ui import __version__
from dataindex_ui.history import (
    MAX_DOC_IDS,
    MAX_TITLE_CHARS,
    ChatHistory,
    ConversationNotFoundError,
    HistoryError,
)
from dataindex_ui.worker import ClientWorker

logger = logging.getLogger("dataindex_ui")

MAX_QUESTION_CHARS = 2_048
MAX_ERROR_CHARS = 1_000
MAX_UPLOAD_NAME_CHARS = 120
MAX_CONNECTION_URL_CHARS = 2_048
MAX_CONNECTION_FIELD_CHARS = 128
MAX_APPROVED_ITEMS = 256
UPLOADS_DIRECTORY = "ui_uploads"
CHAT_ERRORS = (DataIndexError, OSError, TimeoutError, ValueError)
_UNSAFE_NAME_CHARACTERS = re.compile(r"[^A-Za-z0-9._ ()-]")
_STATIC_FILES = {
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
}
_SECURITY_HEADERS = (
    (
        b"content-security-policy",
        b"default-src 'none'; script-src 'self'; style-src 'self'; "
        b"img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
        b"form-action 'none'; frame-ancestors 'none'",
    ),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"cache-control", b"no-store"),
)
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True)
class UISettings:
    """What the app needs to know about where and how it is served."""

    storage_path: Path
    port: int
    model_name: str | None
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    host_names: tuple[str, ...] = ("127.0.0.1", "localhost", "[::1]")
    heartbeat_seconds: float = 2.0

    @property
    def cookie_name(self) -> str:
        # Browsers share cookies between ports, so the port keeps two servers apart.
        return f"dataindex_ui_{self.port}"

    @property
    def allowed_hosts(self) -> frozenset[str]:
        hosts = {f"{name}:{self.port}" for name in self.host_names}
        if self.port == 80:
            hosts.update(self.host_names)
        return frozenset(hosts)

    @property
    def allowed_origins(self) -> frozenset[str]:
        return frozenset(f"http://{host}" for host in self.allowed_hosts)

    @property
    def uploads_path(self) -> Path:
        return self.storage_path / UPLOADS_DIRECTORY


class _NewConversation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE_CHARS)


class _Rename(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)


class _Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    doc_ids: list[str] | None = Field(default=None, max_length=MAX_DOC_IDS)


class _DatabaseConnection(BaseModel):
    """A database server to index. Passwords come only from the environment."""

    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=MAX_CONNECTION_URL_CHARS)
    password_env: str | None = Field(
        default=None, min_length=1, max_length=MAX_CONNECTION_FIELD_CHARS
    )
    db_schema: str | None = Field(
        default=None,
        alias="schema",
        min_length=1,
        max_length=MAX_CONNECTION_FIELD_CHARS,
    )
    name: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE_CHARS)
    allowed_columns: dict[str, list[str]] = Field(
        min_length=1, max_length=MAX_APPROVED_ITEMS
    )
    queries: list[dict[str, Any]] = Field(min_length=1, max_length=MAX_APPROVED_ITEMS)


class LocalOnlyMiddleware:
    """Refuse unexpected ``Host`` headers and add browser security headers."""

    def __init__(self, app: Any, allowed_hosts: Iterable[str]) -> None:
        self._app = app
        self._allowed = frozenset(host.lower() for host in allowed_hosts)

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        host = _header(scope, b"host")
        if host is None or host.lower() not in self._allowed:
            response = Response("Invalid Host header.", status_code=400)
            await self._secured(response)(scope, receive, send)
            return

        async def secured_send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                names = {name for name, _ in message.get("headers", [])}
                extra = [item for item in _SECURITY_HEADERS if item[0] not in names]
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        await self._app(scope, receive, secured_send)

    @staticmethod
    def _secured(response: Response) -> Response:
        for name, value in _SECURITY_HEADERS:
            response.headers[name.decode()] = value.decode()
        return response


def create_app(settings: UISettings, worker: ClientWorker) -> FastAPI:
    """Build the app. It closes ``worker`` when it shuts down."""
    history = ChatHistory(settings.storage_path / "ui_history.sqlite3")
    page = _static_text("index.html")
    assets = {name: _static_text(name) for name in _STATIC_FILES}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await run_in_threadpool(worker.close)

    app = FastAPI(
        title="DataIndex chat",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(LocalOnlyMiddleware, allowed_hosts=settings.allowed_hosts)

    @app.exception_handler(RequestValidationError)
    async def validation_failed(_: Request, error: RequestValidationError) -> JSONResponse:
        # The default handler echoes the submitted values; a connection form may carry secrets.
        detail = [
            {
                "loc": list(item.get("loc", ())),
                "msg": item.get("msg", ""),
                "type": item.get("type", ""),
            }
            for item in error.errors()
        ]
        return JSONResponse({"detail": detail}, status_code=422)

    def require_session(request: Request) -> None:
        cookie = request.cookies.get(settings.cookie_name, "")
        if not secrets.compare_digest(cookie.encode(), settings.token.encode()):
            raise HTTPException(401, "Open the link printed by dataindex-ui to sign in.")
        if request.method in _UNSAFE_METHODS:
            origin = request.headers.get("origin")
            if origin not in settings.allowed_origins:
                raise HTTPException(403, "Requests must come from the DataIndex page.")

    session = [Depends(require_session)]

    @app.get("/", include_in_schema=False)
    def index(request: Request, token: str | None = Query(default=None)) -> Response:
        if token is not None:
            if not secrets.compare_digest(token.encode(), settings.token.encode()):
                return _sign_in_page(status_code=401)
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(
                settings.cookie_name,
                settings.token,
                httponly=True,
                samesite="strict",
                path="/",
            )
            return response
        cookie = request.cookies.get(settings.cookie_name, "")
        if not secrets.compare_digest(cookie.encode(), settings.token.encode()):
            return _sign_in_page(status_code=401)
        return HTMLResponse(page)

    @app.get("/static/{name}", include_in_schema=False)
    def static(name: str) -> Response:
        if name not in assets:
            raise HTTPException(404)
        return Response(assets[name], media_type=_STATIC_FILES[name])

    @app.get("/api/status", dependencies=session)
    def status() -> dict[str, Any]:
        return {
            "version": __version__,
            "model": settings.model_name,
            "chat_enabled": settings.model_name is not None,
            "limits": {
                "question_chars": MAX_QUESTION_CHARS,
                "upload_bytes": MAX_SUBMITTED_BINARY_FILE_BYTES,
                "upload_text_bytes": MAX_SUBMITTED_FILE_BYTES,
                "upload_suffixes": sorted(DOCUMENT_SUFFIXES),
                "doc_ids": MAX_DOC_IDS,
            },
        }

    @app.get("/api/conversations", dependencies=session)
    def list_conversations() -> dict[str, Any]:
        return {"conversations": _history_call(history.list_conversations)}

    @app.post("/api/conversations", dependencies=session, status_code=201)
    def create_conversation(body: _NewConversation | None = None) -> dict[str, Any]:
        title = None if body is None else body.title
        return _history_call(history.create_conversation, title)

    @app.get("/api/conversations/{conversation_id}", dependencies=session)
    def get_conversation(conversation_id: str) -> dict[str, Any]:
        return _history_call(history.get_conversation, conversation_id)

    @app.patch("/api/conversations/{conversation_id}", dependencies=session)
    def rename_conversation(conversation_id: str, body: _Rename) -> dict[str, Any]:
        return _history_call(history.rename_conversation, conversation_id, body.title)

    @app.delete(
        "/api/conversations/{conversation_id}", dependencies=session, status_code=204
    )
    def delete_conversation(conversation_id: str) -> Response:
        if not _history_call(history.delete_conversation, conversation_id):
            raise HTTPException(404, "No such chat.")
        return Response(status_code=204)

    @app.post("/api/conversations/{conversation_id}/messages", dependencies=session)
    async def ask(conversation_id: str, body: _Question) -> Response:
        if settings.model_name is None:
            raise HTTPException(
                409,
                "No chat model is configured. Restart dataindex-ui with --model NAME "
                "or set DATAINDEX_MODEL.",
            )
        doc_ids = body.doc_ids or None
        question = await run_in_threadpool(
            _history_call,
            history.add_message,
            conversation_id,
            "user",
            body.question,
            doc_ids=doc_ids,
        )
        job = worker.submit(
            lambda client: _answer(client, history, conversation_id, body.question, doc_ids)
        )
        return StreamingResponse(
            _answer_events(question, asyncio.wrap_future(job), history, conversation_id,
                           settings.heartbeat_seconds),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @app.get("/api/documents", dependencies=session)
    async def list_documents() -> dict[str, Any]:
        listing = await worker.call(lambda client: client.list_documents(limit=100))
        documents = [
            {key: value for key, value in item.items() if key != "path"}
            for item in listing["documents"]  # type: ignore[union-attr]
            if isinstance(item, Mapping)
        ]
        return {"documents": documents, "total": listing["total"]}

    @app.post("/api/documents", dependencies=session, status_code=201)
    async def upload_document(
        request: Request, filename: str = Query(min_length=1, max_length=255)
    ) -> dict[str, Any]:
        name = _upload_name(filename)
        content = await _bounded_body(request, submitted_file_limit(Path(name)))
        target = settings.uploads_path / name
        existed = await run_in_threadpool(_write_upload, target, content)
        try:
            result, messages = await worker.call(
                lambda client: _submit(client, target, name)
            )
        except CHAT_ERRORS as error:
            if not existed:
                with suppress(OSError):
                    target.unlink()
            raise _client_http_error(error) from error
        return {"document": result, "warnings": messages}

    @app.post("/api/databases", dependencies=session, status_code=201)
    async def connect_database(body: _DatabaseConnection) -> dict[str, Any]:
        try:
            result = await worker.call(
                lambda client: client.submit_database(
                    url=body.url,
                    password_env=body.password_env,
                    schema=body.db_schema,
                    name=body.name,
                    allowed_columns=body.allowed_columns,
                    queries=body.queries,
                )
            )
        except CHAT_ERRORS as error:
            raise _client_http_error(error) from error
        return {"document": dict(result)}

    @app.delete("/api/documents/{doc_id}", dependencies=session, status_code=204)
    async def delete_document(doc_id: str) -> Response:
        try:
            await worker.call(
                lambda client: _delete_document(client, doc_id, settings.uploads_path)
            )
        except CHAT_ERRORS as error:
            raise _client_http_error(error) from error
        return Response(status_code=204)

    return app


def _answer(
    client: DataIndexClient,
    history: ChatHistory,
    conversation_id: str,
    question: str,
    doc_ids: list[str] | None,
) -> dict[str, Any]:
    """Answer on the client thread and save the reply, even if the page went away."""
    try:
        answer = client.chat(question, doc_id=doc_ids)
    except CHAT_ERRORS as error:
        return history.add_message(conversation_id, "error", _error_text(error))
    except Exception as error:
        logger.exception("answering a question failed")
        return history.add_message(
            conversation_id,
            "error",
            f"Unexpected error ({type(error).__name__}); see the dataindex-ui terminal.",
        )
    return history.add_message(
        conversation_id,
        "assistant",
        answer.text,
        doc_ids=doc_ids,
        answer=answer.to_dict(),
    )


async def _answer_events(
    question: dict[str, Any],
    job: asyncio.Future[dict[str, Any]],
    history: ChatHistory,
    conversation_id: str,
    heartbeat_seconds: float,
) -> AsyncIterator[str]:
    # The model replies in one piece, so the stream carries the saved question,
    # progress ticks while the answer is prepared, and then the saved reply.
    yield _event("message", question)
    loop = asyncio.get_running_loop()
    started = loop.time()
    while True:
        done, _ = await asyncio.wait({job}, timeout=heartbeat_seconds)
        if done:
            break
        yield _event("status", {"state": "thinking", "elapsed": round(loop.time() - started)})
    try:
        reply = job.result()
    except HistoryError as error:
        yield _event("error", {"detail": _error_text(error)})
    except Exception:
        logger.exception("saving an answer failed")
        yield _event("error", {"detail": "The answer could not be saved."})
    else:
        yield _event("message", reply)
    with suppress(HistoryError):
        summary = await run_in_threadpool(history.get_conversation, conversation_id)
        summary.pop("messages")
        yield _event("done", {"conversation": summary})


def _submit(
    client: DataIndexClient, path: Path, name: str
) -> tuple[dict[str, Any], list[str]]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        result = client.submit_document(path, name=name)
    return dict(result), [str(item.message) for item in caught]


def _delete_document(client: DataIndexClient, doc_id: str, uploads: Path) -> None:
    document = client.get_document(doc_id)
    client.delete_document(doc_id)
    path = document.get("path")
    if isinstance(path, str):
        uploaded = Path(path).resolve()
        # Only files this interface copied in are removed; others stay untouched.
        if uploaded.parent == uploads.resolve():
            with suppress(OSError):
                uploaded.unlink()


def _write_upload(target: Path, content: bytes) -> bool:
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(4)}.part")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, target)
    finally:
        with suppress(OSError):
            temporary.unlink()
    return existed


def _upload_name(filename: str) -> str:
    base = re.split(r"[\\/]", filename)[-1].strip()
    clean = _UNSAFE_NAME_CHARACTERS.sub("_", base)
    suffix = Path(clean).suffix.lower()
    if suffix not in DOCUMENT_SUFFIXES:
        raise HTTPException(
            415,
            f"Upload a {', '.join(sorted(DOCUMENT_SUFFIXES))} file. Connect database "
            "servers with Connect; submit database files with: dataindex submit-db",
        )
    if clean.startswith(".") or not Path(clean).stem.strip(" ._"):
        raise HTTPException(400, "The file name is not usable.")
    if len(clean) > MAX_UPLOAD_NAME_CHARS:
        clean = Path(clean).stem[: MAX_UPLOAD_NAME_CHARS - len(suffix)] + suffix
    return clean


async def _bounded_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise _too_large(limit)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise _too_large(limit)
        chunks.append(chunk)
    if size == 0:
        raise HTTPException(400, "The file is empty.")
    return b"".join(chunks)


def _too_large(limit: int) -> HTTPException:
    return HTTPException(
        413, f"Files of this type may be at most {limit // (1024 * 1024)} MiB."
    )


def _history_call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return function(*args, **kwargs)
    except ConversationNotFoundError as error:
        raise HTTPException(404, "No such chat.") from error
    except (HistoryError, ValueError) as error:
        raise HTTPException(400, _error_text(error)) from error


def _client_http_error(error: Exception) -> HTTPException:
    if isinstance(error, DocumentNotFoundError):
        return HTTPException(404, "No such document.")
    return HTTPException(400, _error_text(error))


def _error_text(error: BaseException) -> str:
    text = str(error).strip() or type(error).__name__
    return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 1] + "\u2026"


def _event(name: str, data: Mapping[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _header(scope: Mapping[str, Any], name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return None


def _static_text(name: str) -> str:
    return (resources.files("dataindex_ui") / "static" / name).read_text(encoding="utf-8")


def _sign_in_page(*, status_code: int) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><title>DataIndex chat</title>"
        "<p>Open the link that <code>dataindex-ui</code> printed in its terminal. "
        "It contains this session's sign-in token.</p>",
        status_code=status_code,
    )
