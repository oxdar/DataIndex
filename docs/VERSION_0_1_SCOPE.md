# DataIndex v0.1.0 — Scope and release contract

## Status
Greenfield specification, not software. **The first version to create is v0.1.0.** Prior chat references to DataIndex v0.1, v0.2 or a standalone starter are not a baseline and must not be imported or migrated.

## Core hypothesis
An LLM can more reliably navigate a **typed hierarchical catalog + relationship/evidence graph + authorized native tools** than if it is required to embed every source. This is a technical hypothesis, **not** a proven superiority claim; benchmark it.

## v0.1.0 must deliver
1. Installable `dataindex-core` distribution; `import dataindex` works with no extensions, no LLM configured and no network.
2. A versioned public plugin API; independent entry-point discovery, compatibility validation, trust check, enable/disable and health/status lifecycle.
3. Persistent node catalog, hierarchies, source records, relationships, source revisions and native locators; SQLite default in local mode.
4. Free independently installable **DocumentIndex** (`dataindex-ext-documents`): Markdown, TXT, HTML as required minimal formats; PDF optional if tests and dependencies are ready. Natural structure preservation and exact citations.
5. Free independently installable **DataBaseIndex** (`dataindex-ext-databases`): SQLite live bounded queries, schema/keys, approved SELECT and aggregates; PostgreSQL optional in v0.1.0, not automatically claimed.
6. Deterministic retrieval without LLM, plus an optional provider-neutral LLM planner with bounded tool selection and citation-backed answers; one mixed-source demo must pass.
7. Central dispatch policy separating installed, discovered, trusted, compatible, enabled, entitled, tenant-authorized, and healthy states.
8. **Synthetic premium extension fixture** as an *independently built distribution* showing deny→grant→revoke behavior. No production premium capability or real billing provider is required.
9. CLI, minimal SDK, repeatable examples, pytest, CI, dependency/license inventory and release packaging.
10. A build/test matrix verifying core-only and independent plugin installations.

## User-requested scope addition: PageIndex-style local mode
After Phase 07 began, the user asked for setup and usage "very similar to PageIndex". This is an approved addition to the v0.1.0 candidate, not a replacement for the items above, and it is recorded in [ADR-007](./adr/ADR-007-local-pageindex-style-mode.md):

- `DataIndexClient(chat=...)` with `submit_document`, `submit_database`, `get_document`, `list_documents`, `get_document_id`, `get_tree`, `get_section`, `delete_document` and `chat`; matching `dataindex submit|submit-db|documents|chat` commands.
- A new independently packaged adapter, `dataindex-llm` (OpenAI-compatible backend on the standard library; optional LiteLLM extra). Core stays free of provider SDKs and credentials.
- Installed first-party free plugins are trusted by default in local mode; premium and third-party plugins still need an explicit allowlist. Source authorization and license entitlement remain separate central checks.
- Agent tool exports (`as_openai_tools`, `as_anthropic_tools`, `agent_instructions`, `call_tool`) that route through the same dispatcher.
- Not included, and documented as differences from PageIndex: OCR (PDF is
  supported through the documents extension's optional parser extra), streaming,
  cloud mode, MCP export, folders/metadata, a page-based tree, LLM-built summaries.

## User-requested scope addition: local browser chat
The user then asked for a ready-to-use chat interface for people who run DataIndex on their own machine, built as a small FastAPI + Uvicorn app with one static HTML/JS page and SSE, shipped as a separate optional package, with chat history (including message content) saved and listed in a sidebar:

- A new independently packaged front end, `dataindex-ui` (`dataindex-ui` command), that wraps the local `DataIndexClient`. It is not a plugin, and core never imports it or FastAPI.
- Loopback-only, single-user: per-run sign-in token, `Host`/`Origin` checks and a strict Content-Security-Policy. It is not a hosted mode, a REST API for other clients, or a multi-user service.
- Chats are saved in `ui_history.sqlite3` in the workspace. SSE carries progress and the finished answer; model tokens are not streamed, and earlier messages are not sent to the model.

## User-requested scope addition: broad document formats
The user also requested that local document indexing accept common office,
data, text, email and e-book formats in addition to the initial Markdown, TXT
and HTML set. The free `dataindex-ext-documents` wheel provides tested native
structure readers for PDF, legacy and Open XML Office, OpenDocument, RTF, EPUB,
EML, CSV/TSV, JSON/JSONL and XML, as well as Markdown, plain text and HTML.
PDF and legacy Office parsing are optional extras; core remains free of parser
dependencies, and missing extras produce an install hint. This is a bounded,
explicit support list, not a claim of universal format coverage or OCR.

## User-requested scope addition: database servers
The user then requested database support beyond SQLite ("all known databases")
and chose the engine list, SQLAlchemy Core as the SQL driver layer, passwords
through environment variables only, and a UI connect form. The free
`dataindex-ext-databases` wheel adds DuckDB files and PostgreSQL, MySQL,
MariaDB, SQL Server, Oracle Database and MongoDB servers, plus Snowflake,
BigQuery and ClickHouse marked experimental. Each driver is an optional extra;
the base wheel and core keep no driver dependency, and missing drivers produce
an install hint. The same approved typed queries, allowlists, bounds and central
dispatcher checks apply; no arbitrary SQL is accepted. Connection URLs never
contain a password: only the name of a `DATAINDEX_*` environment variable is
stored. A server source is versioned by its reflected schema, and rows are read
live. Live tests against real servers run only in the `database-servers` CI job;
"all known databases" is not claimed, and other engines are refused.

## Explicit exclusions from v0.1.0
- Production paid BIM/CAD/Subsurface/Visual/Geometry/Reality/Geo/Media readers.
- Universal format support or reliable OCR on all scans.
- Hosted billing portal, subscription checkout, marketplace, license service production operations.
- Real enterprise IAM/SSO, unrestricted cross-database joins, high-scale parallel ingestion.
- Guaranteed universal RAG accuracy, geological interpretation, CAD clash detection or model-authoring functions.

These are future product tracks, documented in `docs/EXTENSION_CATALOG.md` and `docs/FUTURE_RELEASES.md`.

## Completion definition
Release v0.1.0 only when all Phase 00–07 gates in `docs/ACCEPTANCE_CRITERIA.md` pass on a clean installation. Proposed distribution names must be checked for publishing availability; no release is implied by this kit.
