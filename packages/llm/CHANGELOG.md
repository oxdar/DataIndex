# Changelog

## 0.1.0

- Initial optional chat model adapter for DataIndex.
- Standard-library OpenAI-compatible HTTP backend (`OpenAICompatibleChatModel`).
- Optional LiteLLM backend (`LiteLLMChatModel`, extra `dataindex-llm[litellm]`).
- `create_chat_model()` factory called by `dataindex.llm.resolve_chat_model()`.
