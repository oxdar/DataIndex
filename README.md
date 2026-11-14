# DataIndex v0.1.0 — Greenfield Copilot Build Kit

**STATUS: Phases 03–06 are implemented and locally validated; their review and
licensing gates remain open. Phase 07 SDK/CLI and release-readiness work is in
progress, including a user-requested PageIndex-style local mode (see
[Quick start](#quick-start-local-mode-pageindex-style)); no release is
authorized.** The standalone `dataindex-core`
package includes a versioned SQLite hierarchy/evidence catalog and a bounded,
provider-neutral reasoning/dispatch API. Independent extensions support
Markdown, plain text, HTML, and bounded read-only SQLite queries, and the
optional `dataindex-llm` package supplies chat models by name. Hosted
authentication, process isolation, and a production entitlement service are
not implemented. The core/free-package license remains pending maintainer/legal
review, so redistribution is blocked.

This is the **first** DataIndex build: start from a blank Git repository. Do not download, import, migrate, or depend on previously discussed DataIndex v0.1/v0.2 starter code. The first implementation produced by GitHub Copilot should be **DataIndex v0.1.0**, built phase-by-phase from this specification.

## Product

DataIndex is a vectorless, reasoning-based, multimodal retrieval framework. A lightweight open-source core exposes a typed hierarchical index, evidence/relationship graph, source locators, a constrained retrieval tool dispatcher, a plugin registry, and optional LLM-guided retrieval. Real data formats live in **independently installable distributions**, some free and some proprietary commercial.

**First release scope (v0.1.0):** independently installable core + free document plugin + free SQL plugin; native no-embedding retrieval; small but real reasoning loop with citations; stable source and plugin contracts; a synthetic private-extension test fixture demonstrating discover / enable / entitlement / authorization gating; CLI, SDK, packaging, tests, CI and docs. Paid production modules (BIM, CAD, Subsurface, etc.) are planned **later releases** and must not be described as implemented in v0.1.0.

## Start here

1. Open this folder in **VS Code** as your new `DataIndex` repository (or copy the kit contents into a new empty repository).
2. Read `docs/VERSION_0_1_SCOPE.md`, `docs/SYSTEM_ARCHITECTURE.md`, `docs/EXTENSION_CONTRACT.md`, `docs/REPOSITORY_AND_PACKAGING.md`, `docs/SECURITY_AND_ENTITLEMENTS.md`, and `docs/IMPLEMENTATION_PLAN.md`.
3. Run `.github/prompts/00-architecture.prompt.md` in **GitHub Copilot Agent mode**.
4. For each following phase, invoke the matching `.github/prompts/NN-*.prompt.md`; require tests and sign-off before proceeding.
5. Treat phase plans as specifications, not proof of implementation. Check
   `docs/PHASE_STATUS.md` for the tested status of each phase; only completed,
   verified slices are implemented.

## Build order

| Phase | Work | v0.1.0 gate |
|---|---|---|
| 00 | Greenfield architecture and standards | No working code assumed |
| 01 | Standalone core, packaging, extension discovery | Core slice implemented; tests and clean wheel install passed; review pending |
| 02 | Native hierarchical/evidence index | Persistent typed catalog and citations implemented; review pending |
| 03 | Free DocumentIndex package | Markdown/TXT/HTML natural-section extraction and cited retrieval verified; review pending, license unresolved |
| 04 | Free DataBaseIndex package | SQLite schema indexing and bounded typed queries verified; review/license gate open |
| 05 | Reasoning, agent SDK and grounded answers | Bounded mixed-source retrieval with citations and abstention |
| 06 | Commercial extension framework (test fixture only) | Independent premium mock requires entitlement |
| 07 | CLI/API, security, CI, release readiness | Candidate `v0.1.0` release with tested wheels; human review and license gates remain |

The synthetic Phase 05 mixed-source example is
[examples/mixed_sources.py](./examples/mixed_sources.py); it runs only after
installing the core, documents, and databases wheels and prints the SQL and
Markdown evidence locators with the answer.

## Quick start: local mode (PageIndex-style)

Local mode follows the [PageIndex](https://github.com/VectifyAI/PageIndex)
workflow: install, point a client at a chat model, submit a file, ask a
question. Retrieval stays vectorless, and an answer is built only from evidence
returned by authorized tools; each citation carries its source ID, version,
native locator and retrieval operation. DataIndex has no PageIndex dependency
and this layer shares no code with it.

### Install

Every package installs on its own: there is no meta-package and no `[all]`
extra. Nothing is published yet, so this is the intended command and is **not
installable today** (names and licenses await review):

```bash
pip install dataindex-core dataindex-ext-documents dataindex-ext-databases dataindex-llm
```

To try it now, build and install the four local wheels. This publishes nothing
and does not authorize redistribution:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install build "packaging>=23.2"
foreach ($package in "core", "extensions\documents", "extensions\databases", "llm") {
    python -m build --wheel --outdir .\dist\local ".\packages\$package"
}
python -m pip install --no-index --find-links .\dist\local dataindex-core dataindex-ext-documents dataindex-ext-databases dataindex-llm
```

`dataindex-core` also works alone; it just has nothing to index. `dataindex-llm`
is only needed to name a chat model as a string: it is a small OpenAI-compatible
HTTP client built on the standard library. For LiteLLM install
`dataindex-llm[litellm]` and use `litellm:<model>` names.

### Python

```python
from dataindex import DataIndexClient

client = DataIndexClient("gpt-4o")      # reads OPENAI_API_KEY
doc_id = client.submit_document("./handbook.md")["doc_id"]
answer = client.chat("What are the payment terms?", doc_id=doc_id)

print(answer)                           # text with [1] markers and a Sources list
for citation in answer.citations:
    print(citation.source_id, citation.source_version, citation.locator, citation.operation)
```

`gpt-4o` and `openai/gpt-4o` use the OpenAI-compatible backend, which reads
`OPENAI_API_KEY`. Set `OPENAI_BASE_URL`, or pass
`chat_backend={"api_base": "...", "api_key": "..."}`, for another
OpenAI-compatible server such as Ollama or vLLM; endpoints must use `https://`
unless they are loopback addresses. Any object with a `complete()` method also
works as the model (see `dataindex.llm.ChatModel`). A missing package or API key
fails when the client is created, not on the first question.

The rest of the PageIndex-style surface:

```python
client.list_documents()                # {"documents": [...], "total", "limit", "offset"}, newest first
client.get_document(doc_id)            # status: completed | stale | missing | unavailable
client.get_document_id("handbook.md")  # by name; DocumentNotFoundError if absent
client.get_tree(doc_id)                # nested outline: titles, node IDs, native locators
client.get_section(doc_id, node_id)    # one section's text and its locator
client.delete_document(doc_id)         # forgets it; the file itself is untouched
client.chat("...")                     # without doc_id, searches every submitted document
```

IDs look like `doc-<16 hex>` (files) or `db-<16 hex>` (databases) and are stable
for an absolute path, so resubmitting is idempotent: `change` is `new`,
`updated` or `unchanged`. A second file with an existing name is kept under a
`_1`-style suffix with a `UserWarning`. Indexed structure lives in `./.dataindex`
(override with `storage_path=`). The source file stays authoritative: it is read
when a question is answered, and a file that changed after submission is
reported as `stale` until you submit it again.

SQLite databases are submitted with the typed, read-only queries you approve.
The model chooses a `query_id` and filter values and never writes SQL, and the
database performs the calculation. The queries are plain JSON (the
`QueryDefinition.to_mapping()` form; `QueryDefinition` objects from
`dataindex_ext_databases` are accepted too):

```python
import json
from pathlib import Path

config = json.loads(Path("examples/contracts_database_config.json").read_text())
db_id = client.submit_database("./contracts.sqlite", **config)["doc_id"]   # allowed_columns + queries
```

To drive retrieval from your own agent loop instead of `chat`:

```python
tools = client.as_openai_tools()            # function-calling schemas (as_anthropic_tools() too)
system = client.agent_instructions()        # prompt to pair with the tools
reply = client.call_tool(name, arguments)   # JSON text for the model; failures return {"error": ...}
```

The tools are `list_documents`, `get_tree`, `get_section`, `search_document` and
`query_database`; `include_management=True` adds `remove_document`. Every call
goes through the same central dispatcher as `chat`.

### Command line

```powershell
$env:OPENAI_API_KEY = "<your key>"
dataindex submit .\handbook.md
dataindex submit-db .\contracts.sqlite --config .\examples\contracts_database_config.json
dataindex documents list
dataindex documents tree handbook.md
dataindex documents section handbook.md <node_id>
dataindex chat "What are the payment terms?" --doc handbook.md --model gpt-4o
dataindex chat --model gpt-4o               # interactive; each question stands alone
dataindex documents delete handbook.md
```

`DOC` arguments accept a document ID or an exact name (an ID wins).
`--storage DIR` selects the workspace (default `./.dataindex`), `--json` prints
machine-readable output, and `--model` falls back to `DATAINDEX_MODEL`. Exit
codes: `0` success (an abstained answer is a success), `2` a handled error,
`130` interrupted.

`submit-db --config FILE` takes a JSON object of at most 256 KiB with exactly two
keys: `allowed_columns` (table name to the columns queries may use) and `queries`
(typed definitions, the JSON form of `QueryDefinition`). The sample
[examples/contracts_database_config.json](./examples/contracts_database_config.json)
matches the SQL fixture
`packages/extensions/databases/examples/fixtures/contracts_invoices.sql`; build a
sample database from it with the standard library:

```powershell
python -c "import pathlib, sqlite3; db = sqlite3.connect('contracts.sqlite'); db.executescript(pathlib.Path('packages/extensions/databases/examples/fixtures/contracts_invoices.sql').read_text()); db.close()"
```

### How it differs from PageIndex

| PageIndex | DataIndex local mode |
|---|---|
| `PageIndexClient(index=..., chat=...)` | `DataIndexClient(chat=..., storage_path=...)`; indexing is deterministic and calls no model, so there is no `index=` |
| PDF documents; hosted cloud mode | Markdown, plain text, HTML and SQLite; no PDF/OCR, no cloud mode, no MCP server in v0.1.0 |
| `submit_document(path, mode=...)` | `submit_document(path)`; indexing is synchronous and `wait` is accepted only for compatibility |
| Page-based tree | Outline with native locators (line ranges), at most 10,000 nodes |
| `chat(messages)`, streaming, `citations=` | `chat(question: str)` with a 2,048-character question, no streaming; citations are always built |
| LLM-built tree and summaries | Natural document structure; no LLM-authored summaries as proof |
| Agent helpers such as `document_context`, `folder_context`, `*_agent_config` | `agent_tools()`, `as_openai_tools()`, `as_anthropic_tools()`, `agent_instructions()` and `call_tool()` only |

### What local mode trusts and sends

- **Plugins:** by default only the installed first-party free plugins
  (`dataindex-ext-documents`, `dataindex-ext-databases`) are trusted, by name and
  installed version. Premium and third-party plugins need
  `trusted_plugins=[("distribution", "version")]`. Plugins are arbitrary Python
  running in your process; the entry-point registry is not a sandbox.
- **Access:** only files you submit are authorized, and entitlement and source
  checks still run before every tool call.
- **Model provider:** `chat` sends your question, document names, section
  outlines (titles and locators) and bounded evidence excerpts to the configured
  model; it never sends local file paths or API keys. Use a local model (for
  example Ollama) for confidential documents.
- **Secrets:** API keys come only from `OPENAI_API_KEY` or `chat_backend`; no
  `.env` file is read. Fixed `source_filters` values in a database config are
  stored in plain text in `workspace.sqlite3`.
- **Scale and concurrency:** one client per thread, and keep `tenant_id` the same
  for a given `storage_path`. A section larger than 256 KiB cannot be returned as
  evidence.

## Catalog CLI (explicit trust flags)

The catalog commands below predate local mode and take explicit trust flags.
They operate on local files and a local SQLite catalog. Their tenant flag is
a catalog label, not authentication; use separate catalog files across trust
boundaries. The document commands require an exact trusted distribution name
and version, plus explicit plugin enablement. Querying also requires the same
file and rejects content whose SHA-256 revision differs from the indexed
revision. These commands do not accept SQL text.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install build "packaging>=23.2"
python -m build --wheel --outdir .\dist\quickstart packages\core
python -m build --wheel --outdir .\dist\quickstart packages\extensions\documents
python -m pip install --no-index --find-links .\dist\quickstart dataindex-core==0.1.0 dataindex-ext-documents==0.1.0
dataindex index --catalog .\dataindex.sqlite --tenant local --source-id handbook --source-type text/markdown --file .\README.md --trusted-distribution dataindex-ext-documents --trusted-version 0.1.0 --enable-plugin
dataindex query --catalog .\dataindex.sqlite --tenant local --source-id handbook --source-type text/markdown --file .\README.md --trusted-distribution dataindex-ext-documents --trusted-version 0.1.0 --enable-plugin --text "vectorless retrieval"
dataindex sources list --catalog .\dataindex.sqlite --tenant local
dataindex extensions inspect documents
```

These commands build and install local validation wheels; they do not publish
the packages or authorize redistribution. PyPI installation is intentionally
omitted until package names and licenses are approved.

See the [Python API reference](./docs/PYTHON_API.md) for SDK policy hooks and
the current database-extension limitations.

The Phase 06 commercial contract is tested using only the synthetic,
independently built wheel under [tests/plugins/mock_premium](./tests/plugins/mock_premium).
It exercises entitlement denial, grant, expiry, revocation, tenant isolation,
and continued document/SQLite tool execution; it is not a production premium
implementation or licensing service.

Post-0.1: Visual, Geometry, BIM/CAD, Subsurface, Geospatial, Reality and Media extensions. These belong to **separate free/paid packages** and are NOT v0.1.0 completion gates.

## Phase 00 initial prompt

The original first prompt below documents how this build kit was initialized.
Phase 00 has been completed for progression to Phase 01; consult
`docs/PHASE_STATUS.md` for current validation evidence.

> Read `AGENTS.md`, `.github/copilot-instructions.md`, `docs/VERSION_0_1_SCOPE.md`, and all architecture, security, extension, and phase documents. This is a completely NEW project: no pre-existing DataIndex implementation and no migration. Start with **Phase 00** only. Design the greenfield v0.1 core, installable free extensions, and a commercial extension contract. Inspect the upstream PageIndex repository for source code that can be lawfully adapted directly into DataIndex. Do not add a runtime PageIndex dependency. Produce architecture decisions, documents and acceptance tests for Phase 00. Do not claim to have implemented later phases.

## Source / license note

PageIndex upstream: https://github.com/VectifyAI/PageIndex (MIT). The core
contains a narrow adaptation of its hierarchy parent-assignment routine; see
`THIRD_PARTY_NOTICES.md` for the pinned commit, transformation and license text.
It has no PageIndex runtime dependency.
