"""Optional LiteLLM chat backend; install with ``dataindex-llm[litellm]``."""

from __future__ import annotations

import importlib
import os
from collections.abc import Mapping, Sequence
from typing import Any

from dataindex.exceptions import ChatModelConfigurationError, ChatModelError
from dataindex.llm import ChatMessage, ChatResult

from dataindex_llm.openai_compatible import (
    _is_token_count,
    _message_payload,
    _require_seconds,
    _require_text,
    _require_token_limit,
    _Secret,
    _with_status_code,
)

_NOT_INSTALLED = 'LiteLLM is not installed; run: pip install "dataindex-llm[litellm]"'


class LiteLLMChatModel:
    """Chat model that delegates to ``litellm.completion``.

    LiteLLM resolves provider credentials from its own environment variables
    when no ``api_key`` is given, and applies its own URL handling to
    ``api_base``. LiteLLM is imported with ``LITELLM_MODE=PRODUCTION`` unless the
    caller already set a mode, so it does not read a ``.env`` file; see the README.
    Provider error text is never copied into DataIndex errors.
    """

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        api_base: str | None = None,
        module: Any | None = None,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ChatModelConfigurationError(
                "the LiteLLM model name must not be empty"
            )
        _require_text("api_key", api_key)
        _require_text("api_base", api_base)
        loaded = module if module is not None else _import_litellm()
        if not callable(getattr(loaded, "completion", None)):
            raise ChatModelConfigurationError(
                "the LiteLLM module does not provide a completion function"
            )
        self._model = model.strip()
        self._key = _Secret(api_key.strip()) if api_key is not None else None
        self._api_base = api_base.strip() if api_base is not None else None
        self._module = loaded

    @property
    def model(self) -> str:
        return self._model

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self._model!r})"

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int,
        timeout_seconds: float,
        json_output: bool = True,
    ) -> ChatResult:
        """Run one completion; unsupported parameters are dropped by LiteLLM."""
        payload = _message_payload(messages)
        _require_token_limit(max_output_tokens)
        seconds = _require_seconds(timeout_seconds)
        if not isinstance(json_output, bool):
            raise ChatModelError("json_output must be a boolean")
        arguments: dict[str, Any] = {
            "model": self._model,
            "messages": payload,
            "max_tokens": max_output_tokens,
            "timeout": seconds,
            "temperature": 0,
            "drop_params": True,
            "num_retries": 0,
        }
        if json_output:
            arguments["response_format"] = {"type": "json_object"}
        if self._key is not None:
            arguments["api_key"] = self._key.reveal()
        if self._api_base is not None:
            arguments["api_base"] = self._api_base
        try:
            response = self._module.completion(**arguments)
        except Exception as error:
            raise _request_error(error) from None
        return _parse_response(response)


def _import_litellm() -> Any:
    # LiteLLM loads a .env file on import unless LITELLM_MODE says otherwise. When
    # the caller has not chosen a mode, set one only for the length of the import.
    chosen = "LITELLM_MODE" in os.environ
    if not chosen:
        os.environ["LITELLM_MODE"] = "PRODUCTION"
    try:
        return importlib.import_module("litellm")
    except ImportError as error:
        raise ChatModelConfigurationError(_NOT_INSTALLED) from error
    except Exception as error:
        raise ChatModelConfigurationError(
            f"LiteLLM could not be imported ({type(error).__name__})"
        ) from None
    finally:
        if not chosen:
            os.environ.pop("LITELLM_MODE", None)


def _request_error(error: Exception) -> ChatModelError:
    """Name the failure by class only; LiteLLM messages may carry request details."""
    mapped = ChatModelError(f"LiteLLM request failed: {type(error).__name__}")
    try:
        status = getattr(error, "status_code", None)
    except Exception:
        status = None
    if isinstance(status, int) and not isinstance(status, bool):
        _with_status_code(mapped, status)
    return mapped


def _field(source: object, name: str) -> object:
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def _parse_response(response: object) -> ChatResult:
    try:
        choices = _field(response, "choices")
        first = choices[0] if isinstance(choices, (list, tuple)) and choices else None
        content = _field(_field(first, "message"), "content")
        total = _field(_field(response, "usage"), "total_tokens")
    except Exception:
        raise ChatModelError("the LiteLLM response could not be read") from None
    if not isinstance(content, str):
        raise ChatModelError("the chat model returned no text content")
    return ChatResult(content, total if _is_token_count(total) else None)
