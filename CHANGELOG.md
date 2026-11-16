# Changelog

All notable changes are recorded here. This file describes the unreleased
first-implementation candidate; it is not evidence that a package has shipped.

## Unreleased — v0.1.0 candidate

- Added a typed `DataIndexClient` façade for source registration, bounded
  extension indexing, tenant-scoped metadata search, retrieval, planner-backed
  `ask`, and extension status.
- Added local CLI extension inspection, tenant-scoped source listing, bounded
  metadata search, and explicitly trusted/explicitly enabled document
  `index`/`query` commands.
- Added Python API and CLI security documentation and release-matrix CI for
  separate package wheels and sdists.
- Added a PageIndex-style local mode (user-requested):
  `DataIndexClient(chat=...)` with `submit_document`, `submit_database`,
  `get_document`, `list_documents`, `get_document_id`, `get_tree`,
  `get_section`, `delete_document` and a cited, abstaining `chat`; a local
  workspace under `./.dataindex`; agent tool exports (`as_openai_tools`,
  `as_anthropic_tools`, `agent_instructions`, `call_tool`) that run through the
  central dispatcher; and `dataindex submit|submit-db|documents|chat` commands.
  Installed first-party free plugins are trusted by default in local mode only;
  premium and third-party plugins still need an explicit allowlist.
- Added the independently packaged `dataindex-llm` adapter (standard-library
  OpenAI-compatible backend; optional LiteLLM extra) and a sample database
  configuration, `examples/contracts_database_config.json`.
- Added the optional, independently packaged `dataindex-ui` (user-requested): a
  loopback-only FastAPI + Uvicorn chat page with one static HTML/JS page, SSE
  progress, citation cards, document upload and removal, and a sidebar of saved
  chats whose full content is kept in `ui_history.sqlite3` in the workspace.
- Expanded the free documents extension to tested office, PDF, OpenDocument,
  RTF, EPUB, EML, CSV/TSV, JSON/JSONL and XML readers, with optional parser
  extras and bounded native-structure extraction.
- Expanded the free databases extension beyond SQLite: DuckDB files and
  PostgreSQL, MySQL, MariaDB, SQL Server, Oracle Database and MongoDB servers
  (integration tier, with live CI contract tests), plus experimental Snowflake,
  BigQuery and ClickHouse. Each driver is an optional extra. Connections are
  password-free URLs with a `DATAINDEX_*` password environment variable;
  `submit_database(url=..., password_env=..., schema=...)`, `dataindex
  submit-db --url` and a UI Connect form register them with the same approved
  typed queries, and arbitrary SQL is still not accepted.
- Plugin exceptions raised during tool execution are now reported as
  `ToolExecutionError`, and `DataIndexClient.open` clients reject local-workflow
  calls with `ConfigurationError`.
- Hardened local mode after test review: the planner reply parser no longer uses
  a backtracking regular expression for code fences (a reply of an opening fence
  and a few thousand blanks ran for minutes), `SQLiteCatalog` reports an
  unreadable or corrupt catalog as `CatalogError`, a claim that cites evidence
  that was never retrieved raises `InvalidCitationError`, and stale-file
  messages no longer name SDK methods so they read correctly in the SDK and the
  CLI.
- Release remains blocked on open Phase 03–06 human reviews and the unresolved
  core/free-package and extension license decisions. No package is published and
  the local mode supports PDF only through the documents extension's optional
  parser extra; it makes no OCR, streaming, cloud or MCP claims.
