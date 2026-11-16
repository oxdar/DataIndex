# dataindex-core

- [Overview](#overview)
- [Python API reference](#python-api-reference)
  - [Host mode](#host-mode-dataindexclientopen)
  - [Local mode](#local-mode-pageindex-style)
  - [Submitting and inspecting](#submitting-and-inspecting)
  - [Chat](#chat)
  - [Chat models](#chat-models)
  - [Agent tools](#agent-tools)
  - [Trust, authorization and data flow](#trust-authorization-and-data-flow)
- [CLI reference](#catalog-cli-and-local-mode-cli)
- [Versioning and support](#versioning-and-support)

## Overview

The DataIndex core package. It provides typed source/evidence models, a
versioned SQLite catalog, bounded hierarchy/graph navigation, a small
`DataIndexClient` SDK, and a local CLI for extension inspection, source listing,
document indexing and retrieval. A PageIndex-style local mode lets you submit
documents, SQLite and DuckDB files and database server connections, and ask
cited questions with
`DataIndexClient("gpt-4o").chat(...)` or `dataindex chat`; it needs the free
extensions and, for model names, the optional `dataindex-llm` package (see the
[repository installation guide](../../README.md#installation)).

Core has no built-in format readers and does not import extension modules during
discovery. Entry-point code is loaded only when its distribution/version is
explicitly trusted, compatible with the core API and enabled by the operator.
Trusted Python plugins are executable code, not sandboxed code.
The current trust allowlist compares distribution name/version only; the
installer is responsible for verifying artifact provenance or signatures.

The public extension API is currently `0.1`. The core also exposes a bounded,
provider-neutral `ReasoningEngine`, deterministic hierarchical source
selection, a centralized `ToolDispatcher`, structured planning contracts,
citation validation, and no-source abstention. Model providers are supplied by
applications or by the optional `dataindex-llm` package; core does not install or
contact a model provider on its own. The host must
provide tenant authorization and an optional `EntitlementProvider`; commercial
calls are checked against the configured deployment and return typed denial
reason codes. Core does not provide a billing service or persistent audit
storage.
Tool evidence is checked for source/version/operation provenance and catalog
locator consistency, but claim-text entailment and re-reading non-catalog
sources remain application responsibilities. `SQLiteCatalog` stores source
revisions, typed nodes, evidence and graph edges.
Source content remains authoritative and outside the catalog; evidence is
accepted only after its native locator is resolved by a caller-provided
`LocatorDereferencer`. Catalog keyword search and graph traversal are
deterministic and bounded. Relationship identity confidence is an optional
0-to-1 value; `None` means it was not measured. Core ships no document or
database readers; install extensions separately. Tenant labels prevent
cross-tenant graph edges but do not authorize catalog reads; use separate
catalog files across trust boundaries.
SQLite evidence values are not encrypted by core.

`DataIndexClient.open()` requires a host-provided `SourceAuthorizer`, trusted
distribution/version allowlist, enabled plugin IDs, and deployment identifier.
Indexing and retrieval are bounded and extension code remains trusted in-process
Python, not sandboxed. `DataIndexClient(chat=...)` is the single-user local mode:
it trusts only the installed first-party free plugins by default (premium and
third-party plugins need an explicit `trusted_plugins` entry), authorizes only the
files you submit, and keeps its index and workspace under `./.dataindex`. The
catalog CLI is for local single-user operation: its tenant
argument is only a catalog label, not authentication. Document `index` and
`query` commands require explicit `--trusted-distribution`,
`--trusted-version`, and `--enable-plugin` arguments. Neither CLI executes
arbitrary SQL; databases are submitted with approved typed queries
(`dataindex submit-db PATH --config FILE`, or `--url URL --password-env NAME`
for a database server; the password itself never appears in a URL or argument).

The repository's core/free-extension license choice is still pending maintainer
review, so this initial build does not declare a distribution license.

## Python API reference

The core distribution is `dataindex-core`; its import namespace is `dataindex`.
The core imports neither optional extension packages nor model-provider SDKs.
Chat models by name come from the separate, optional `dataindex-llm`
distribution (see [Chat models](#chat-models)).

`DataIndexClient` has two entry points that share one dispatcher:

- [`DataIndexClient(chat=...)`](#local-mode-pageindex-style) is the local,
  PageIndex-style workflow: submit a file, then chat about it.
- [`DataIndexClient.open(...)`](#host-mode-dataindexclientopen) is the host
  entry point with an explicit authorizer, allowlist and entitlement provider.

### Host mode: `DataIndexClient.open`

`DataIndexClient.open(catalog_path=":memory:", *, tenant_id,
source_authorizer, trusted_distributions=(), enabled_plugin_ids=(),
entitlement_provider=None, deployment)` creates a client, reads extension
metadata, and loads only plugins that are compatible, explicitly trusted by
distribution name/version, and explicitly enabled. `deployment` is mandatory
for entitlement decisions. Use the client as a context manager to close loaded
extensions and the catalog. A client built this way has no local workflow:
calling `submit_document`, `chat` or another local method raises
`ConfigurationError`.

The host must implement `SourceAuthorizer.is_authorized(tenant_id, source,
capability)`. It must enforce source access as well as capability access. A
tenant ID is an application-supplied identity label; the core does not
authenticate users or provide process isolation.

Public operations:

- `register_source(source)` requires host `source.access` authorization, then
  creates or advances a tenant-owned source revision. Advancing a revision
  invalidates the previous active nodes and evidence.
- `extension(plugin_id)` returns a loaded, trusted, enabled, compatible,
  healthy extension or raises `ToolNotAvailableError`.
- `index(source, plugin_id, *, timeout_seconds=30, maximum_records=10000)`
  reads typed nodes from the extension, verifies source/version identity, and
  stores them atomically as one batch. It requires the host to authorize the
  extension's single declared `*.index` capability. The deadline is
  cooperative; it cannot stop a blocked in-process extension. The host
  provides source bytes/connection configuration directly to the extension.
- `search(source, query, *, limit=100)` searches current catalog node metadata
  after tenant and `source.access` authorization.
- `get_source(source_id)` returns a source only when it belongs to the client
  tenant and passes host `source.access` authorization. `list_sources(*,
  limit=100)` applies the same authorization while keeping the result bounded.
- `retrieve(PlannedToolCall, *, timeout_seconds=30, maximum_results=100)`
  sends a typed request through the central dispatcher, which rechecks source
  ownership, authorization, trust, health, entitlement, request shape and
  evidence provenance. An exception raised by a plugin tool is reported as
  `ToolExecutionError` (the original is its `__cause__`), because plugin
  exceptions are not `DataIndexError` subclasses.
- `ask(question, sources, planner, *, budget=ReasoningBudget())` runs the
  bounded planner loop. A planner is provider-neutral and must return
  source-scoped tool calls and claims citing returned evidence IDs.
- `list_extensions()` returns discovery/load statuses without changing trust.

The SDK does not store raw source content, accept model-authored SQL, install
plugins, or sandbox plugin code. Database query definitions and allowlists must
be supplied to the database extension through its typed contract; arbitrary
SQL is not a supported SDK operation.

### Local mode (PageIndex-style)

`DataIndexClient(chat=None, *, chat_backend=None, storage_path="./.dataindex",
tenant_id="local", trusted_plugins=None, instructions=None)` creates a local
client. It follows the shape of
[PageIndex](https://github.com/VectifyAI/PageIndex): submit a file, receive a
`doc_id`, ask questions. This workflow does not import PageIndex or depend on
its runtime.
The client is a context manager, is not thread-safe (use one per thread), and
keeps one `tenant_id` per `storage_path`: sources belong to the tenant that
registered them, and a different tenant raises `SourceAuthorizationError`.

Install the core, documents extension and model adapter using the
[repository installation guide](../../README.md#installation), then configure
your [chat model](#chat-models). Run this example from the repository root:

```python
from dataindex import DataIndexClient

with DataIndexClient(chat="gpt-4o") as client:
    doc_id = client.submit_document(
        "packages/extensions/documents/tests/fixtures/payment_terms.md"
    )["doc_id"]
    answer = client.chat("What are the payment terms?", doc_id=doc_id)
    print(answer)
```

| Argument | Meaning |
|---|---|
| `chat` | `None` for a submit/inspect-only client, a model name (needs `dataindex-llm`), or any object with `complete()` ([`ChatModel`](#chat-models)). Names are resolved when the client is created, so a missing package, key or bad name fails there and not on the first question. |
| `chat_backend` | Only `api_key` and/or `api_base`, and only together with a named `chat`; otherwise `ChatModelConfigurationError`. |
| `storage_path` | Directory for `catalog.sqlite3` (the index) and `workspace.sqlite3` (submitted documents). It is created on first use. |
| `tenant_id` | Label that owns the sources in `storage_path`. Keep it constant. |
| `trusted_plugins` | `None` trusts the installed first-party free plugins (`dataindex-ext-documents`, `dataindex-ext-databases`) by name and installed version. A name trusts the installed version, a `(name, version)` pair trusts exactly that version, `[]` trusts nothing, and a list replaces the default. A single string is rejected. Premium and third-party plugins run only when listed. |
| `instructions` | Default extra instructions for `chat` (at most 4,000 characters). They are added to a fixed system prompt; retrieved text is always passed to the model as untrusted data, never as instructions. |

#### Submitting and inspecting

- `submit_document(path, *, name=None, wait=True)` indexes a supported document
  suffix listed in the [documents extension README](../extensions/documents/README.md#supported-formats).
  Text input is limited to 5 MiB and binary input to 50 MiB. PDF and the legacy
  `.doc`, `.xls` and `.ppt` readers require the corresponding optional extras;
  without one, submission reports its installation hint. It returns
  `{"doc_id", "name", "status": "completed", "change",
  "source_type", "version", "node_count"}`. `change` is `"new"`, `"updated"` or
  `"unchanged"`: the ID is derived from the absolute path (`doc-<16 hex>`), so
  resubmitting is idempotent. Indexing is synchronous; `wait` exists only for
  PageIndex compatibility and has no effect. A name already used by another
  document gets a `_1`, `_2`, ... suffix before the extension (up to 99) and a
  `UserWarning`.
- `submit_database(path=None, *, allowed_columns, queries, name=None, url=None,
  password_env=None, schema=None)` registers a database with the typed
  read-only queries you approve and returns the same shape with `db-<16 hex>`.
  Pass exactly one of `path` or `url`:
  - `path` is a SQLite or DuckDB file (`.sqlite`, `.sqlite3`, `.db`, `.duckdb`,
    `.ddb`; the file header decides which engine). DuckDB needs the
    `dataindex-ext-databases[duckdb]` extra.
  - `url` is a database server without a password, such as
    `postgresql://reader@db.example.com/sales`. Put the password in an
    environment variable whose name starts with `DATAINDEX_` and pass that name
    as `password_env`; a URL that contains a password raises `ValueError`.
    `schema` selects a schema where the engine has them. Engines, URL formats
    and their driver extras are listed in the
    [databases extension README](../extensions/databases/README.md#supported-engines).
    The ID is derived from the password-free URL and schema, so resubmitting
    the same server is idempotent. Submitting contacts the server. An invalid
    URL, missing driver, unset password variable or unreachable server raises
    `SourceRejectedError` with the plugin's (password-free) message; a missing
    or outdated databases plugin raises `ConfigurationError`.

  `queries` holds `QueryDefinition` objects or their `to_mapping()`
  dictionaries; the approved configuration (with the password-free connection
  for servers) is stored as JSON (at most 256 KiB) and is part of the source
  version. There is no raw SQL entry point.
- `get_document(doc_id)` returns `{"id", "doc_id", "name", "description",
  "status", "createdAt", "kind", "source_type", "plugin_id", "version",
  "path"}` plus `detail` when the status is not `completed`, and a whitelisted
  `queries` summary for databases. A database server has `path: null` and a
  `connection` object with `engine`, `url` and, when set, `schema` and
  `password_env` (the variable name, never its value). `status` is
  `completed`, `stale` (the file, or a server's schema, changed after
  submission), `missing` or `unavailable` (for example the plugin is not
  installed, or a server cannot be reached). `path` is for the local user; it
  is never shown to agents or sent to a model.
- `list_documents(limit=50, offset=0)` returns `{"documents", "total", "limit",
  "offset"}`, newest first, with `1 <= limit <= 100`.
- `get_document_id(name)` returns the ID for an exact name or raises
  `DocumentNotFoundError`.
- `get_tree(doc_id, *, max_entries=None)` returns `{"doc_id", "name", "status",
  "truncated", "result"}`; `result` is a nested outline whose nodes are
  `{"node_id", "title", "kind", "locator": {"scheme", "version", "value"},
  "summary"?, "nodes"}`. Outlines hold at most 10,000 nodes and `truncated`
  reports a cut.
- `get_section(doc_id, node_id)` returns `{"doc_id", "name", "node_id",
  "title", "locator", "text", "source_version", "operation", "evidence_id"}` for
  one document section. Documents only (a database raises `ConfigurationError`);
  an unknown `node_id` raises `NodeNotFoundError`, and a section above 256 KiB
  raises `ToolEvidenceTooLargeError`.
- `delete_document(doc_id)` removes the document from the workspace and its
  index and returns whether it existed. The source file is never touched.

`DocumentNotFoundError` reports an unknown ID or name. `SourceFileChangedError`
means the file was edited or removed after submission: submit it again. A file
the plugin refuses raises `SourceRejectedError`. Bad argument values raise
`ValueError`; setup, plugin and unsupported-file problems raise
`ConfigurationError`; an unreadable or newer workspace raises `WorkspaceError`.
Plugin exceptions are not `DataIndexError` subclasses, so they surface as
`SourceRejectedError` (while submitting) or `ToolExecutionError` (while
answering).

Indexed structure lives in the workspace; **the source file stays
authoritative**. Reading content (`get_tree`, `get_section`, `chat`) first
re-checks the file against the version recorded at submission, so edited content
is never served from an old index; `get_document` and `list_documents` report
the same condition as `stale`. If a plugin release changes how a file is
indexed, an unchanged file keeps its old nodes until you delete and resubmit it.
A live SQLite or DuckDB database whose file keeps changing (for example in WAL
mode) needs resubmitting after each change. A database server is different: its
version follows the reflected schema of the allowlisted tables and views (a
view's columns, not its SQL text), not the rows,
so queries always read current rows and each result carries its read
timestamp. Listing, opening or querying a server source reflects its schema
again, which takes a round trip to the server; a schema change reports `stale`
until you resubmit.

#### Chat

`chat(question, doc_id=None, *, budget=None, instructions=None)` returns a
`ChatAnswer`.

- `question` is a string of 1 to 2,048 characters. `doc_id` is one ID, a
  sequence of 1 to 64 IDs, or `None` for every submitted document; with `None`
  and more than 64 documents, `ConfigurationError` asks you to choose with
  `doc_id=[...]`. Streaming and message lists are not supported.
- With explicit IDs every document must be usable. With `doc_id=None`, documents
  that cannot be read (`stale`, `missing`, `unavailable`) are skipped and named
  in `ChatAnswer.warnings`.
- An empty workspace, or one with nothing readable, returns an abstained answer
  without calling the model.
- The default budget is 3 reasoning depth, 8 tool calls, 100,000 tokens, 120
  seconds and 100 results. A `ReasoningBudget` may change those up to the core
  caps (depth 8, calls 32, tokens 128,000, 300 s, 100 results).
- The model only plans tool calls and states claims that cite evidence IDs.
  Retrieval, calculation and every access check are deterministic code in the
  dispatcher. The model's reply must be one strict JSON object; a malformed
  reply, an unknown tool or a citation of evidence that was not returned is sent
  back for one repair attempt and then raises `InvalidPlannerOutputError`.
  Parameters that name the source or its version are injected by DataIndex and
  cannot be overridden. A reply with no claims yields an abstained answer
  (`abstained=True` with a `reason`) rather than a guess. A failed tool call is
  reported to the model by error type only.

`ChatAnswer` is a `str` subclass whose text is the answer with `[1]`-style
markers followed by a `Sources:` list, so `print(answer)` works. Attributes:
`text`, `claims` (`AnswerClaim`: `text` and the `citations` numbers it uses),
`citations`, `evidence` (the dispatcher's `EvidenceRecord` values), `sources`,
`abstained`, `reason`, `tokens_used`, `depth` and `warnings`; `to_dict()`
returns a JSON-compatible form. Each `Citation` carries `number`, `evidence_id`,
`source_id`, `source_version`, `document` (the display name), `operation`,
`evidence_kind`, `locator`, `position`, `node_id`, a clipped `excerpt` and
`excerpt_truncated`. Source ID, version, native locator and operation are always
present. Model summaries and inferred links are not factual proof; the cited
excerpt is.

#### Chat models

`ChatModel` is a small protocol: `complete(messages, *, max_output_tokens,
timeout_seconds, json_output=True) -> ChatResult` where `ChatResult` has `text`
and `tokens_used`. Any object that implements it works, so a test double or a
custom provider needs no extra package. For model names, core lazily imports
`dataindex_llm.create_chat_model(name, **options)`:

- `gpt-4o` and `openai/gpt-4o` use the standard-library OpenAI-compatible
  backend. It reads only `OPENAI_API_KEY` and `OPENAI_BASE_URL` (no `.env`
  file). URLs must be `https://`, or `http://` for `localhost`, `127.0.0.1` and
  `::1` (where the key is optional, for example Ollama or vLLM). Redirects are
  refused, the key is redacted from errors, and responses are bounded.
- `litellm:<model>` selects LiteLLM and needs the optional `litellm` extra of
  `dataindex-llm`. See the [installation guide](../../README.md#optional-extras)
  for installing extras from local wheels.
- Other prefixes are sent to the OpenAI-compatible backend unchanged.

A factory failure is reported as `ChatModelConfigurationError` naming only the
exception class, never provider text or credentials. Runtime backend failures
raise `ChatModelError`.

#### Agent tools

To run retrieval from your own agent loop, use the same authorized tools `chat`
uses: `agent_tools(*, include_management=False)`,
`as_openai_tools(...)`, `as_anthropic_tools(...)`,
`agent_instructions(...)` and `call_tool(name, arguments=None, *,
include_management=False) -> str`. The tools are `list_documents`, `get_tree`,
`get_section`, `search_document` and `query_database`; `include_management=True`
adds `remove_document`. `call_tool` accepts a JSON string or a mapping and
returns bounded JSON text. A refused, invalid or failed call returns
`{"error": "<fixed message>"}` so the model can recover; programming errors still
raise. Results never include file paths. There is no MCP export in v0.1.0, and
PageIndex helpers such as `document_context`, `folder_context` and the
`*_agent_config` bundles are not provided.

#### Trust, authorization and data flow

- Local mode trusts only the plugins named by `trusted_plugins` (by default the
  installed first-party free plugins) and enables them without further flags. A
  missing or untrusted plugin is reported and does not stop the others. Loaded
  plugins are arbitrary Python running in your process; this is not a sandbox.
- A local authorizer grants access only to sources you submitted, pinned to one
  source ID, type and version, and only the capabilities of a free, unlicensed
  plugin. Source authorization and license entitlement are still separate
  checks in the central dispatcher.
- `chat` sends the question, document names, section outlines, tool schemas and
  bounded evidence excerpts to the configured model. It never sends local paths,
  API keys or workspace contents beyond that. Choose a local model for
  confidential files.
- Fixed `source_filters` values in a database configuration are stored in the
  workspace as plain text; protect the storage directory with OS permissions.

## Catalog CLI and local mode CLI

The `dataindex` command has two families of subcommands. They never accept SQL
text. Document files and catalogs are local, but operations on registered
database servers (including status checks and retrieval) contact those servers.
`chat` also contacts the configured model.

### Local mode commands

`dataindex submit PATH`, `submit-db PATH --config FILE`, `submit-db --url URL
[--password-env NAME] [--schema NAME] --config FILE`, `documents
list|get|tree|section|delete` and `chat [QUESTION]` mirror the Python methods
above. They share `--storage DIR` (default `./.dataindex`) and, for output,
`--json`. A `DOC` argument is an ID or an exact name, and an ID wins. `chat`
takes `--doc DOC` (repeatable), `--model NAME` (or `DATAINDEX_MODEL`) and
`--instructions TEXT`. Without a question it reads one per line from standard
input; each question is independent. `submit-db` takes exactly one of `PATH`
or `--url`; `--password-env` names the `DATAINDEX_*` variable that holds the
server password, which is never passed on the command line. The `--config`
file must be a JSON object with exactly `allowed_columns` and `queries`, at
most 256 KiB;
[examples/contracts_database_config.json](../../examples/contracts_database_config.json)
is a sample. Exit codes are 0 for success (an abstained answer is a success), 2
for a handled error and 130 for Ctrl+C. Messages are sanitized before display.
There is no `--trust`, `--tenant` or `--api-key` flag: trust follows
`trusted_plugins` defaults and keys come only from `OPENAI_API_KEY`.

### Catalog commands

`dataindex extensions list|inspect` performs metadata-only discovery.
`dataindex sources list --catalog PATH --tenant ID` lists only that tenant's
source references. `dataindex nodes search` performs bounded catalog metadata
search and requires a tenant and source ID.

`dataindex index` and `dataindex query` support the document suffixes listed in
the extension README, with 5 MiB text and 50 MiB binary limits. Formats that
need optional parser extras must have those extras installed. Both commands require
`--trusted-distribution`, `--trusted-version` and `--enable-plugin`. The source
revision is a SHA-256 digest of the supplied file bytes. `query` requires the
same file and rejects stale content. Retrieval returns one section with source
ID, version, native locator and operation.

The CLI is single-user local tooling. Its `--tenant` value is not an
authentication mechanism, the catalog file must be protected by OS permissions,
and shared deployments need a separately enforced server authorization layer.
The catalog commands intentionally have no arbitrary SQL input or database query
command; databases are submitted through local mode with approved queries.

## Versioning and support

The public extension API is currently `0.1`. The supported core runtime floor
is Python 3.11. `dataindex-llm` follows the same 0.1 line and depends on
`dataindex-core>=0.1,<0.2`. This v0.1.0 candidate does not include hosted
authentication, plugin isolation, a production entitlement service, OCR, or
production BIM/CAD/Subsurface extensions. The release is blocked until
maintainers approve the unresolved license and earlier phase review gates.
