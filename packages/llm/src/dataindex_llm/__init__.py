"""Optional chat model adapters for DataIndex.

Core calls :func:`create_chat_model` when a user passes a model name.
"""

from __future__ import annotations

from dataindex_llm.factory import create_chat_model
from dataindex_llm.litellm_backend import LiteLLMChatModel
from dataindex_llm.openai_compatible import OpenAICompatibleChatModel

__version__ = "0.1.0"

__all__ = [
    "LiteLLMChatModel",
    "OpenAICompatibleChatModel",
    "__version__",
    "create_chat_model",
]
