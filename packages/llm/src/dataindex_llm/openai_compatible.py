"""OpenAI-compatible chat completions over the Python standard library."""

from __future__ import annotations

import contextlib
import http.client
import json
import math
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import IO, Any, NamedTuple, TypeGuard

from dataindex.exceptions import ChatModelConfigurationError, ChatModelError
from dataindex.llm import ChatMessage, ChatResult

DEFAULT_API_BASE = "https://api.openai.com/v1"
DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024

_OPENAI_HOST = "api.openai.com"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_MAX_RETRIES = 3
_ADJUSTABLE_STATUSES = frozenset({400, 422})
_MAX_ERROR_BYTES = 64 * 1024
_CHUNK_BYTES = 64 * 1024
_MAX_SOCKET_TIMEOUT_SECONDS = 3600.0
_MAX_PROVIDER_MESSAGE_CHARS = 200
_PRINTABLE_ASCII = re.compile(r"[\x21-\x7e]+")
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}")
_STATUS_REASONS = {
    401: "authentication or permission failed",
    403: "authentication or permission failed",
    404: "not found",
    429: "rate limited",
}
_SWAPPED_TOKEN_PARAMETER = {
    "max_tokens": "max_completion_tokens",
    "max_completion_tokens": "max_tokens",
}


class _Secret:
    """Holds a credential so that repr() of any object referring to it is safe."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<redacted>"


class _Endpoint(NamedTuple):
    base: str
    host: str
    loopback: bool
    secure: bool


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Turn redirects into errors; urllib would otherwise forward credentials."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


@dataclass
class _Parameters:
    """Request parameters that a server may reject, each adjusted at most once."""

    token_parameter: str
    response_format: bool = True
    temperature: bool = True
    token_parameter_swapped: bool = False

    def body(
        self, model: str, messages: list[dict[str, str]], max_output_tokens: int
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"model": model, "messages": messages}
        if self.temperature:
            body["temperature"] = 0
        body[self.token_parameter] = max_output_tokens
        if self.response_format:
            body["response_format"] = {"type": "json_object"}
        return body

    def adjust(self, error_text: str) -> bool:
        """Adjust only the parameters the error names; report whether any changed."""
        text = error_text.lower()
        changed = False
        if self.response_format and "response_format" in text:
            self.response_format = False
            changed = True
        if not self.token_parameter_swapped and (
            "max_tokens" in text or "max_completion_tokens" in text
        ):
            self.token_parameter = _SWAPPED_TOKEN_PARAMETER[self.token_parameter]
            self.token_parameter_swapped = True
            changed = True
        if self.temperature and "temperature" in text:
            self.temperature = False
            changed = True
        return changed


class OpenAICompatibleChatModel:
    """Chat model for OpenAI-compatible ``/chat/completions`` endpoints.

    Works with OpenAI, OpenRouter, vLLM, Ollama, LM Studio and similar servers.
    Remote endpoints must use HTTPS; plain HTTP is accepted for localhost only.
    The API key never appears in ``repr()`` or in error messages. Environment
    variables are read from ``environ`` (default ``os.environ``); ``.env``
    files are never read.
    """

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        api_base: str | None = None,
        environ: Mapping[str, str] | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ChatModelConfigurationError("the chat model name must not be empty")
        _require_text("api_key", api_key)
        _require_text("api_base", api_base)
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes < 1
        ):
            raise ChatModelConfigurationError(
                "max_response_bytes must be a positive integer"
            )
        variables = os.environ if environ is None else environ
        base_url: str | None
        if api_base is not None:
            base_url, base_source = api_base, "api_base"
        else:
            base_url = _variable(variables, "OPENAI_BASE_URL")
            base_source = "OPENAI_BASE_URL"
        endpoint = _parse_api_base(base_url or DEFAULT_API_BASE, base_source)
        key = api_key if api_key is not None else _variable(variables, "OPENAI_API_KEY")
        if key is None and not endpoint.loopback:
            raise ChatModelConfigurationError(
                "no API key is configured; set the OPENAI_API_KEY environment "
                'variable or pass chat_backend={"api_key": ...} (a key is '
                "optional only for servers on localhost)"
            )
        self._model = model.strip()
        self._endpoint = endpoint
        self._key = _Secret(_clean_key(key)) if key is not None else None
        self._max_response_bytes = max_response_bytes
        self._token_parameter = (
            "max_completion_tokens" if endpoint.host == _OPENAI_HOST else "max_tokens"
        )
        self._url = f"{endpoint.base}/chat/completions"
        self._user_agent = _user_agent()
        self._opener: urllib.request.OpenerDirector | None = None

    @property
    def model(self) -> str:
        return self._model

    @property
    def api_base(self) -> str:
        return self._endpoint.base

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(model={self._model!r}, "
            f"api_base={self._endpoint.base!r})"
        )

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int,
        timeout_seconds: float,
        json_output: bool = True,
    ) -> ChatResult:
        """Run one chat completion within ``timeout_seconds`` in total.

        If a server answers HTTP 400/422 and names ``response_format``, the
        token-limit parameter or ``temperature``, only that parameter is
        dropped or swapped and the request is repeated, at most three times
        and always against the original deadline.
        """
        payload = _message_payload(messages)
        _require_token_limit(max_output_tokens)
        seconds = _require_seconds(timeout_seconds)
        if not isinstance(json_output, bool):
            raise ChatModelError("json_output must be a boolean")
        deadline = time.monotonic() + seconds
        parameters = _Parameters(self._token_parameter, response_format=json_output)
        retries = 0
        while True:
            body = json.dumps(
                parameters.body(self._model, payload, max_output_tokens),
                separators=(",", ":"),
            ).encode("ascii")
            status, data = self._exchange(body, deadline)
            if 200 <= status < 300:
                return _parse_result(data)
            message, searchable = _error_text(data)
            if (
                retries < _MAX_RETRIES
                and status in _ADJUSTABLE_STATUSES
                and parameters.adjust(searchable)
            ):
                retries += 1
                continue
            raise self._status_error(status, message)

    def _exchange(self, body: bytes, deadline: float) -> tuple[int, bytes]:
        """POST once and return the HTTP status with a size-limited body."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _timed_out()
        request = urllib.request.Request(
            self._url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": self._user_agent,
            },
            method="POST",
        )
        if self._key is not None:
            # Unredirected headers are never copied to another URL.
            request.add_unredirected_header(
                "Authorization", f"Bearer {self._key.reveal()}"
            )
        try:
            response = self._get_opener().open(
                request, timeout=min(remaining, _MAX_SOCKET_TIMEOUT_SECONDS)
            )
        except urllib.error.HTTPError as error:
            try:
                return _error_reply(error, deadline)
            finally:
                with contextlib.suppress(Exception):
                    error.close()
        except Exception as error:
            raise _transport_error(error) from None
        with response:
            return response.status, _read_body(
                response, deadline, self._max_response_bytes
            )

    def _get_opener(self) -> urllib.request.OpenerDirector:
        if self._opener is None:
            handlers: list[urllib.request.BaseHandler] = [_NoRedirectHandler()]
            if self._endpoint.loopback:
                # Local servers must never be reached through an environment proxy.
                handlers.append(urllib.request.ProxyHandler({}))
            if self._endpoint.secure:
                handlers.append(
                    urllib.request.HTTPSHandler(context=ssl.create_default_context())
                )
            self._opener = urllib.request.build_opener(*handlers)
        return self._opener

    def _status_error(self, status: int, message: str | None) -> ChatModelError:
        reason = _STATUS_REASONS.get(status)
        if reason is None:
            reason = "server error" if status >= 500 else "request rejected"
        text = f"the chat endpoint returned HTTP {status} ({reason})"
        if message is not None:
            shown = _shown_message(
                message, self._key.reveal() if self._key is not None else None
            )
            if shown:
                text += f": {shown}"
        return _with_status_code(ChatModelError(text), status)


def _user_agent() -> str:
    # Imported lazily because the package __init__ imports this module.
    from dataindex_llm import __version__

    return f"dataindex-llm/{__version__}"


def _variable(variables: Mapping[str, str], name: str) -> str | None:
    value = variables.get(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _require_text(name: str, value: object) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ChatModelConfigurationError(f"{name} must be a non-empty string")


def _clean_key(value: str) -> str:
    key = value.strip()
    if _PRINTABLE_ASCII.fullmatch(key) is None:
        raise ChatModelConfigurationError(
            "the API key must be one token of printable ASCII characters"
        )
    return key


def _parse_api_base(value: str, name: str) -> _Endpoint:
    """Validate a base URL without ever echoing it, as it could hold secrets."""
    url = value.strip()
    if _PRINTABLE_ASCII.fullmatch(url) is None:
        raise ChatModelConfigurationError(
            f"{name} must be ASCII text without spaces or control characters"
        )
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        _ = parts.port
    except ValueError:
        raise ChatModelConfigurationError(f"{name} is not a valid URL") from None
    if "@" in parts.netloc:
        raise ChatModelConfigurationError(
            f"{name} must not contain credentials; pass api_key instead"
        )
    if "?" in url or "#" in url:
        raise ChatModelConfigurationError(
            f"{name} must not contain a query string or fragment"
        )
    if not host:
        raise ChatModelConfigurationError(f"{name} must include a host name")
    loopback = host in _LOOPBACK_HOSTS
    if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
        raise ChatModelConfigurationError(
            f"{name} must use https; http is allowed only for localhost, "
            "127.0.0.1 and ::1"
        )
    return _Endpoint(url.rstrip("/"), host, loopback, parts.scheme == "https")


def _message_payload(messages: object) -> list[dict[str, str]]:
    if (
        isinstance(messages, (str, bytes))
        or not isinstance(messages, Sequence)
        or not messages
    ):
        raise ChatModelError("messages must be a non-empty sequence of ChatMessage")
    if not all(isinstance(message, ChatMessage) for message in messages):
        raise ChatModelError("messages must contain only ChatMessage objects")
    return [{"role": message.role, "content": message.content} for message in messages]


def _require_token_limit(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ChatModelError("max_output_tokens must be a positive integer")


def _require_seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ChatModelError("timeout_seconds must be a positive finite number")
    try:
        seconds = float(value)
    except OverflowError:
        seconds = math.inf
    if not math.isfinite(seconds) or seconds <= 0:
        raise ChatModelError("timeout_seconds must be a positive finite number")
    return seconds


def _timed_out() -> ChatModelError:
    return ChatModelError("the chat request timed out")


def _transport_error(error: BaseException) -> ChatModelError:
    """Describe a transport failure by class only; OS error text can hold URLs."""
    reason: object = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, TimeoutError):
        return _timed_out()
    name = type(reason if isinstance(reason, BaseException) else error).__name__
    if isinstance(error, (OSError, http.client.HTTPException)):
        return ChatModelError(f"could not reach the chat endpoint ({name})")
    return ChatModelError(f"the chat request failed ({name})")


def _with_status_code(error: ChatModelError, status: int) -> ChatModelError:
    setattr(error, "status_code", status)
    return error


def _read_body(
    response: Any, deadline: float, limit: int, *, truncate: bool = False
) -> bytes:
    """Read at most ``limit`` bytes, checking the deadline between reads."""
    chunks: list[bytes] = []
    size = 0
    while True:
        if time.monotonic() >= deadline:
            raise _timed_out()
        try:
            # read1 returns as soon as any data arrives; read(n) would block
            # until n bytes were buffered and skip the deadline check.
            chunk = response.read1(min(_CHUNK_BYTES, limit + 1 - size))
        except Exception as error:
            raise _transport_error(error) from None
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            if not truncate:
                raise ChatModelError("the chat response is too large")
            break
    return b"".join(chunks)[:limit]


def _error_reply(error: urllib.error.HTTPError, deadline: float) -> tuple[int, bytes]:
    status = error.code
    if 300 <= status < 400:
        raise _with_status_code(
            ChatModelError(
                "the chat endpoint tried to redirect; redirects are not followed"
            ),
            status,
        ) from None
    try:
        data = _read_body(error, deadline, _MAX_ERROR_BYTES, truncate=True)
    except ChatModelError:
        data = b""
    return status, data


def _error_text(data: bytes) -> tuple[str | None, str]:
    """Return the provider's error message (if structured) and text to scan."""
    text = data.decode("utf-8", errors="replace")
    try:
        document = json.loads(text)
    except (ValueError, RecursionError):
        return None, text
    message: object = None
    parameter: object = None
    if isinstance(document, dict):
        error = document.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            parameter = error.get("param")
        elif isinstance(error, str):
            message = error
        else:
            message = document.get("message")
    if isinstance(message, str) and message.strip():
        if isinstance(parameter, str):
            return message, f"{message} {parameter}"
        return message, message
    return None, text


def _shown_message(message: str, secret: str | None) -> str:
    """Redact, flatten to one printable line and truncate a provider message."""
    if secret:
        message = message.replace(secret, "[redacted]")
    message = _BEARER_TOKEN.sub("Bearer [redacted]", message)
    flat = " ".join("".join(c if c.isprintable() else " " for c in message).split())
    if len(flat) > _MAX_PROVIDER_MESSAGE_CHARS:
        return flat[: _MAX_PROVIDER_MESSAGE_CHARS - 3].rstrip() + "..."
    return flat


def _reject_constant(name: str) -> object:
    raise ValueError(f"{name} is not valid JSON")


def _parse_result(data: bytes) -> ChatResult:
    try:
        document = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise ChatModelError("the chat response is not valid JSON") from None
    if not isinstance(document, dict):
        raise ChatModelError("the chat model returned no text content")
    choices = document.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else None
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        refusal = message.get("refusal") if isinstance(message, dict) else None
        if isinstance(refusal, str) and refusal.strip():
            raise ChatModelError("the chat model refused to answer")
        raise ChatModelError("the chat model returned no text content")
    return ChatResult(content, _tokens_used(document.get("usage")))


def _is_token_count(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _tokens_used(usage: object) -> int | None:
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if _is_token_count(total):
        return total
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if _is_token_count(prompt) and _is_token_count(completion):
        return prompt + completion
    return None
