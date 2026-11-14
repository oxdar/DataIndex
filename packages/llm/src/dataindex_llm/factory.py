"""Model-name routing for :func:`create_chat_model`."""

from __future__ import annotations

from dataindex.exceptions import ChatModelConfigurationError
from dataindex.llm import ChatModel

from dataindex_llm.litellm_backend import LiteLLMChatModel
from dataindex_llm.openai_compatible import OpenAICompatibleChatModel

LITELLM_PREFIX = "litellm:"
OPENAI_PREFIX = "openai/"


def create_chat_model(
    model: str,
    *,
    api_key: str | None = None,
    api_base: str | None = None,
) -> ChatModel:
    """Build a chat model from a model name.

    ``litellm:<name>`` selects the optional LiteLLM backend. Any other name uses
    the OpenAI-compatible HTTP backend; one leading ``openai/`` is removed and
    other prefixes such as ``meta-llama/Llama-3`` are sent to the server as-is.
    """
    if not isinstance(model, str) or not model.strip():
        raise ChatModelConfigurationError("the chat model name must not be empty")
    name = model.strip()
    if name.startswith(LITELLM_PREFIX):
        target = name[len(LITELLM_PREFIX) :].strip()
        if not target:
            raise ChatModelConfigurationError(
                f"the model name after {LITELLM_PREFIX!r} must not be empty"
            )
        return LiteLLMChatModel(target, api_key=api_key, api_base=api_base)
    if name.startswith(OPENAI_PREFIX):
        name = name[len(OPENAI_PREFIX) :].strip()
        if not name:
            raise ChatModelConfigurationError(
                f"the model name after {OPENAI_PREFIX!r} must not be empty"
            )
    return OpenAICompatibleChatModel(name, api_key=api_key, api_base=api_base)
