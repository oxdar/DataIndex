# ADR-007: PageIndex-style local mode and the `dataindex-llm` adapter

- **Status:** Proposed; user-requested scope addition awaiting maintainer review
- **Date:** 2026-11-14

## Context

The user asked for DataIndex setup and usage to be "very similar to" the public
[PageIndex](https://github.com/VectifyAI/PageIndex) quickstart: install,
construct a client with a chat model, submit a file, ask a question. The Phase 07
SDK was host-oriented (`DataIndexClient.open` needs an authorizer, a trust
allowlist, enabled plugin IDs and a deployment name), so a first answer required
a page of policy code. The architectural rules still apply: core installs and
imports without provider SDKs, credentials or plugins; plugins are trusted
arbitrary code; source authorization and license entitlement are separate checks
in the central dispatcher; no embeddings, vector search or model-authored SQL;
every answer carries a source ID, version, native locator and operation.

The user chose these directions when asked: no meta-package and no `[all]` extra
(one install line naming four distributions), a separate `dataindex-llm`
distribution with an OpenAI-compatible default and an optional LiteLLM extra,
trust of installed first-party free plugins by default in local mode, and the
extras of document management, tree/section access, CLI commands, typed
database submission and agent tool exports.

## Decision

1. **Local constructor over the same engine.** `DataIndexClient(chat=None, *,
   chat_backend=None, storage_path="./.dataindex", tenant_id="local",
   trusted_plugins=None, instructions=None)` builds the same registry,
   `ToolDispatcher` and `ReasoningEngine` as host mode, plus a `LocalRuntime`.
   `DataIndexClient.open(...)` is unchanged and has no local workflow. Methods:
   `submit_document`, `submit_database`, `get_document`, `list_documents`,
   `get_document_id`, `get_tree`, `get_section`, `delete_document`, `chat`, and
   the agent tool exports.
2. **Install shape.** The documented install is `pip install dataindex-core
   dataindex-ext-documents dataindex-ext-databases dataindex-llm`. There is no
   meta-package and no `[all]` extra; each distribution stays independently
   installable, versioned and removable.
3. **`dataindex-llm` is a separate distribution.** Its default backend is an
   OpenAI-compatible client written with the standard library (`https://` only,
   or `http://` for loopback; no redirects; bounded responses; keys redacted;
   only `OPENAI_API_KEY` and `OPENAI_BASE_URL` read, no `.env`). The optional
   `dataindex-llm[litellm]` extra adds LiteLLM and is selected with
   `litellm:<model>` names. Core only defines the `ChatModel` protocol and
   lazily imports `dataindex_llm.create_chat_model` when given a model name; a
   missing package, key or bad name fails when the client is constructed.
4. **Local trust default (`first_party_default`).** With `trusted_plugins=None`,
   local mode trusts the installed `dataindex-ext-documents` and
   `dataindex-ext-databases` distributions by name at their installed version and
   enables them. A given list replaces the default, `[]` trusts nothing, and
   premium or third-party plugins are never trusted unless listed. A
   `LocalAuthorizer` grants access only to sources the user submitted, pinned to
   one source ID, type and version, and only the capabilities of a free
   plugin that has no capability entitlement. License entitlement is still checked
   separately by the dispatcher. Local-mode code imports no `dataindex_ext_*`
   module; it reaches the plugins through structural protocols.
5. **Workspace.** `storage_path` holds `catalog.sqlite3` (the index) and
   `workspace.sqlite3` (document ID, display name, absolute path, source type,
   version, and for databases the approved-query configuration). Document IDs are
   `doc-`/`db-` plus 16 hex characters of a SHA-256 of the normalized absolute
   path, so resubmitting is idempotent. The source file stays authoritative:
   content reads re-verify the file against the recorded version and refuse to
   serve stale content.
6. **Databases through typed queries only.** `submit_database(path,
   allowed_columns=..., queries=...)` stores the approved `QueryDefinition`
   mappings and registers a read-only source; the configuration digest is part of
   the source version. The model chooses a `query_id` and filter values; it never
   writes SQL.
7. **Chat.** `ModelPlanner` asks the model for one strict JSON object per turn
   (`call_tools` with tool refs, or `answer` with claims citing evidence IDs).
   Source IDs and versions are injected by DataIndex, retrieved text is passed
   as untrusted data, and failed calls reach the model by error type only.
   `ChatAnswer` is a `str` subclass with numbered citations (source ID, version,
   native locator, operation, clipped excerpt) and abstains when no claim is
   supported. Question length, instruction length, budgets, document counts and
   evidence sizes are bounded.
8. **Agent tools.** `agent_tools`, `as_openai_tools`, `as_anthropic_tools`,
   `agent_instructions` and `call_tool` expose `list_documents`, `get_tree`,
   `get_section`, `search_document` and `query_database` (and `remove_document`
   only with `include_management=True`). Every call goes through the same
   dispatcher; results are bounded JSON text without file paths, and refusals are
   fixed error messages.
9. **CLI.** `dataindex submit`, `submit-db --config`, `documents
   list|get|tree|section|delete` and `chat`, with `--storage`, `--json` and
   `--model`/`DATAINDEX_MODEL`. The earlier `index`/`query` catalog commands keep
   their explicit trust flags. There is no `--trust`, `--tenant`, `--api-key` or
   SQL flag.
10. **Documented differences from PageIndex.** No PDF/OCR, page-based tree,
    LLM-built tree or summaries, streaming, message-list chat, `index=`
    argument, cloud mode, folders or metadata queries, `document_context` /
    `folder_context` helpers or MCP export. `wait` on `submit_document` is
    accepted and ignored because indexing is synchronous. No PageIndex code is
    copied or imported by this layer, so `THIRD_PARTY_NOTICES.md` is unchanged.

## Consequences

- The default local trust rule is by distribution name and installed version. A
  package installed under a first-party name is trusted, so installs must come
  from a verified index or pinned artifact. This is a deliberate local-mode
  convenience that relaxes the "explicit allowlist" wording for the two free
  first-party packages only; maintainers must approve it. Hosted deployments keep
  explicit trust and server-side authorization.
- `chat` sends the question, document names, section outlines, tool schemas and
  bounded evidence excerpts to the configured provider. File paths and API keys
  are never sent. Confidential data needs a local model or an approved provider.
- Fixed `source_filters` values in a database configuration are stored in plain
  text in `workspace.sqlite3`; the storage directory needs OS permissions.
- The client is not thread-safe and keeps one `tenant_id` per `storage_path`.
- A plugin release that changes indexing does not re-index an unchanged file; the
  user deletes and resubmits it. A live SQLite database that keeps changing needs
  resubmission.
- Plugin exceptions are not `DataIndexError` subclasses; the dispatcher reports
  them as `ToolExecutionError` and submission reports `SourceRejectedError`.
- A new package (`dataindex-llm`) needs its own CI workflow, release matrix
  entry, PyPI name check and license decision.
- The `dataindex-llm[litellm]` backend has not been run against a real LiteLLM
  install in this workspace; its adapter is covered by fakes only.

## Alternatives rejected

- A meta-package or `[all]` extra would recreate a megawheel and block
  independent versioning (ADR-006).
- Bundling provider SDK code in core would add dependencies, credentials and
  provider behavior to a core that must import with none.
- Trusting every discovered entry point in local mode is trust-on-discovery
  (ADR-004).
- Letting the model write SQL, call arbitrary URLs or run shell/Python violates
  the retrieval boundary; only typed, approved operations are exposed.
- Copying PageIndex's page tree, PDF handling or cloud client would add
  provenance, licensing and dependency obligations outside the v0.1.0 scope.
