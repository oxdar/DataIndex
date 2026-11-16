# DataIndex

**Vectorless retrieval over documents and databases, with source-backed citations.**

DataIndex indexes the native structure of your documents and the schemas of your
databases. It does not create embeddings, vector indexes or fixed-size token
chunks. You can retrieve evidence deterministically, or let a chat model choose
bounded retrieval tools and answer with citations you can check. DataIndex works
from Python, from the command line, or in an optional local browser chat.

> [!IMPORTANT]
> **Pre-release.** DataIndex v0.1.0 is still in development and has not been
> released. The packages are not published on PyPI, and the license for the core
> and free packages has not been chosen. Builds from this repository are for
> local evaluation only and must not be redistributed.

[Features](#features) · [Installation](#installation) · [Quick start](#quick-start) ·
[Python API](#python-api) · [Databases](#databases) · [Browser chat](#browser-chat) ·
[Privacy and limits](#privacy-security-and-limits) · [Documentation](#documentation)

## Features

- **No vectors.** Documents are indexed by their own structure: headings,
  pages, sheets, slides and records. Databases are indexed by the schema of the
  tables and views you allow. No embedding model, vector database or chunking
  step is involved.
- **Common document formats.** PDF, Word, Excel, PowerPoint, OpenDocument, RTF,
  Markdown, plain text, HTML, CSV/TSV, JSON/JSON Lines, XML, EPUB and email
  (`.eml`). See the [full format list](packages/extensions/documents/README.md#supported-formats).
- **Databases without arbitrary SQL.**
  - SQLite and DuckDB files.
  - PostgreSQL, MySQL, MariaDB, SQL Server, Oracle and MongoDB servers.
  - Experimental: Snowflake, BigQuery and ClickHouse.

  The model can run only the read-only queries you approve in advance,
  including bounded joins, `COUNT` and `SUM`. The database performs every
  calculation. See the [engine list](packages/extensions/databases/README.md#supported-engines).
- **Checkable citations.** Each piece of evidence records the source ID, the
  source version, a native locator and the retrieval operation that produced
  it.
- **Your choice of model.** Use an OpenAI-compatible API, a local server such as
  Ollama, the optional LiteLLM backend, or your own `ChatModel` implementation.
  Indexing and browsing documents never need a model.
- **Install only what you need.** The core package imports without a model
  credential, network access, PDF libraries or database drivers. Readers, the
  model adapter and the UI are separate packages. Parsers and database drivers
  are optional extras.
- **Extensible.** Free, third-party and premium readers all implement the same
  typed [extension contract](docs/EXTENSION_CONTRACT.md) and are discovered
  through Python entry points.

### Packages

| Package | Purpose |
| --- | --- |
| [`dataindex-core`](packages/core/README.md) | Catalog, evidence model, plugin runtime, authorization dispatcher, Python SDK and the `dataindex` CLI |
| [`dataindex-ext-documents`](packages/extensions/documents/README.md) | Indexing and retrieval for the supported document formats, using each format's own structure |
| [`dataindex-ext-databases`](packages/extensions/databases/README.md) | Schema indexing and approved read-only queries for SQLite, DuckDB and database servers |
| [`dataindex-llm`](packages/llm/README.md) | Chat-model adapter. The OpenAI-compatible backend uses only the standard library |
| [`dataindex-ui`](packages/ui/README.md) | Optional local browser chat with uploads, citations and saved history |

Each package is a separate wheel; there is no all-in-one package.

## Installation

You need **Python 3.11 or later** and Git. The packages are not on PyPI yet, so
you build wheels from this repository and install them into a virtual
environment. Building downloads the build tools from PyPI.

Clone the repository and create a virtual environment.

**macOS and Linux:**

```bash
git clone https://github.com/oxdar/dataindex.git
cd dataindex
python3 -m venv .venv
source .venv/bin/activate
```

**Windows (PowerShell):**

```powershell
git clone https://github.com/oxdar/dataindex.git
Set-Location dataindex
python -m venv .venv          # or: py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Then build and install the packages. These commands are the same in both shells:

```shell
python -m pip install build "packaging>=23.2"
python -m build --wheel --outdir dist/local packages/core
python -m build --wheel --outdir dist/local packages/extensions/documents
python -m build --wheel --outdir dist/local packages/extensions/databases
python -m build --wheel --outdir dist/local packages/llm
python -m pip install --no-index --find-links dist/local dataindex-core==0.1.0 dataindex-ext-documents==0.1.0 dataindex-ext-databases==0.1.0 dataindex-llm==0.1.0
python -m pip check
```

`--no-index` makes pip install only the wheels you just built, so a package
with the same name on PyPI cannot be picked up by mistake. `packaging`, the only
core dependency, is installed first for that reason.

Install the extensions from wheels, not with `pip install -e`. Plugin discovery
reads a manifest that is packaged inside each wheel. Leave out the packages you
don't need: `dataindex-core` on its own has no document or database readers, and
`dataindex-llm` is needed only for chat.

### Optional extras

Install an extra from its wheel. pip downloads the extra's dependencies from
PyPI:

```shell
python -m pip install "dist/local/dataindex_ext_documents-0.1.0-py3-none-any.whl[all]"
python -m pip install "dist/local/dataindex_ext_databases-0.1.0-py3-none-any.whl[postgresql]"
```

| Package | Extra | Adds |
| --- | --- | --- |
| `dataindex-ext-documents` | `pdf` | PDF (`pypdf`) |
| | `legacy-office` | `.doc`, `.xls` and `.ppt` (`olefile`, `xlrd`) |
| | `all` | All optional document readers |
| `dataindex-ext-databases` | `duckdb` | DuckDB files |
| | `postgresql`, `mysql`, `mssql`, `oracle`, `mongodb` | Server drivers. `mysql` also covers MariaDB. SQL Server also needs the Microsoft ODBC Driver 17 or 18 |
| | `snowflake`, `bigquery`, `clickhouse` | Experimental server drivers |
| | `all` | All database drivers |
| `dataindex-llm` | `litellm` | Optional LiteLLM backend |

If a file needs a reader that isn't installed, DataIndex reports which extra to
install. The other formats keep working.

## Quick start

Run these commands from the repository root with the virtual environment
active.

### 1. Index a document (no model needed)

The repository includes a short synthetic
[invoice payment guide](packages/extensions/documents/tests/fixtures/payment_terms.md):

```shell
dataindex submit packages/extensions/documents/tests/fixtures/payment_terms.md
dataindex documents list
dataindex documents tree payment_terms.md
```

Indexing runs locally and makes no model calls. The workspace lives in
`.dataindex` in the current directory; use `--storage DIR` to choose another
one. The original file stays the authoritative source and must remain
accessible. Resubmit a file after it changes.

Other document commands:

- `dataindex documents section <doc> <node-id>` prints the text of one section.
- `dataindex documents get <doc>` prints a document's details as JSON.
- `dataindex documents delete <doc>` removes a document from the workspace. The
  original file is not touched.

Add `--help` to any command for all of its options.

### 2. Ask a question

To use OpenAI, set your API key and ask a question.

**macOS and Linux:**

```bash
export OPENAI_API_KEY="<your key>"
dataindex chat "When are invoices due?" --doc payment_terms.md --model gpt-4o
```

**Windows (PowerShell):**

```powershell
$env:OPENAI_API_KEY = "<your key>"
dataindex chat "When are invoices due?" --doc payment_terms.md --model gpt-4o
```

The guide says invoices are due net 30 days after receipt. The answer includes
numbered citations and a list of sources. Omit the question to start an
interactive session, and set `DATAINDEX_MODEL` to avoid repeating `--model`.
Model output varies. Citation checks confirm where each piece of evidence came
from, but they do not guarantee that every claim in the answer is correct.

To use a local OpenAI-compatible server such as [Ollama](https://ollama.com)
instead, start the server, make sure the model is available, and point
DataIndex at the server.

**macOS and Linux:**

```bash
unset OPENAI_API_KEY
export OPENAI_BASE_URL="http://localhost:11434/v1"
dataindex chat "When are invoices due?" --doc payment_terms.md --model llama3.1
```

**Windows (PowerShell):**

```powershell
Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
$env:OPENAI_BASE_URL = "http://localhost:11434/v1"
dataindex chat "When are invoices due?" --doc payment_terms.md --model llama3.1
```

Loopback endpoints don't need a key; remote endpoints must use HTTPS. The
adapter sends your API key to whatever `OPENAI_BASE_URL` points at, so check
that variable before setting a real key. The built-in adapter does not read
`.env` files. See the [model adapter guide](packages/llm/README.md) for all
settings and the LiteLLM backend.

## Python API

```python
from dataindex import DataIndexClient

with DataIndexClient(chat="gpt-4o") as client:
    document = client.submit_document(
        "packages/extensions/documents/tests/fixtures/payment_terms.md"
    )
    answer = client.chat("When are invoices due?", doc_id=document["doc_id"])
    print(answer)

    for citation in answer.citations:
        print(
            citation.source_id,
            citation.source_version,
            citation.locator,
            citation.operation,
        )
```

- Use `chat="llama3.1"` if you set up a local server.
- `DataIndexClient()` without a model can still submit and inspect documents.
- `list_documents()`, `get_tree()`, `get_section()` and `delete_document()`
  manage the workspace. Deleting a document never deletes the source file.
- Submitting the same path again keeps its document ID and updates the indexed
  version when the file has changed.

To build your own agent loop, use `as_openai_tools()`, `as_anthropic_tools()`,
`agent_instructions()` and `call_tool()`. Host applications can call
`DataIndexClient.open(...)` with explicit authorization and trust policies. The
[Python API reference](docs/PYTHON_API.md) documents signatures, errors, budgets
and citation fields.

## Databases

A registered database has two parts:

- **An allowlist** (`allowed_columns`) of the tables, views and columns that can
  be read.
- **Approved queries** (`queries`). Each query declares its table, columns,
  joins, aggregate, ordering and row limit, the filters the model may fill in,
  and any fixed filters it may not change.

The model only picks a query ID and supplies values for the declared filters.
The database does the calculation, and no table rows are indexed. Views can be
allowlisted like tables; see [Views](packages/extensions/databases/README.md#views).

### Try the sample database

The repository includes a [synthetic SQL fixture](packages/extensions/databases/examples/fixtures/contracts_invoices.sql)
and a matching [query configuration](examples/contracts_database_config.json).
Create the sample database once, then register it:

```shell
python -c "import pathlib, sqlite3; db = sqlite3.connect('contracts.sqlite'); db.executescript(pathlib.Path('packages/extensions/databases/examples/fixtures/contracts_invoices.sql').read_text(encoding='utf-8')); db.close()"
dataindex submit-db contracts.sqlite --config examples/contracts_database_config.json
dataindex documents tree contracts.sqlite
dataindex chat "What is the unpaid invoice total per contract for invoices due by 2026-02-28?" --doc contracts.sqlite --model gpt-4o
```

The example below combines the document and the database and answers
deterministically. It needs **no API key or model server**:

```shell
python examples/mixed_sources.py
```

From Python, call `client.submit_database(path, allowed_columns=..., queries=...)`.

### Connect to a database server

Submit a server by URL instead of a path. First install its driver extra (see
[Optional extras](#optional-extras)). The URL never contains the password. Store
the password in an environment variable whose name starts with `DATAINDEX_` and
pass that variable's name.

**macOS and Linux:**

```bash
export DATAINDEX_SALES_PASSWORD="..."
dataindex submit-db --url postgresql://reader@db.example.com/sales --password-env DATAINDEX_SALES_PASSWORD --config sales_config.json
```

**Windows (PowerShell):**

```powershell
$env:DATAINDEX_SALES_PASSWORD = "..."
dataindex submit-db --url postgresql://reader@db.example.com/sales --password-env DATAINDEX_SALES_PASSWORD --config sales_config.json
```

From Python, call `client.submit_database(url=..., password_env=..., schema=...,
allowed_columns=..., queries=...)`. The browser UI has a matching **Connect**
form.

Use a login that can only read the allowlisted tables and views. DataIndex
checks the server's schema again each time the source is listed, opened or
queried, so the server must be reachable. The
[database guide](packages/extensions/databases/README.md#connecting-to-a-database-server)
covers URL formats, TLS options, read-only guards, query shapes, result limits
and source-scope filters.

## Browser chat

The optional UI adds document uploads, database connections, source selection,
citation cards and saved chats. After [installation](#installation), run:

```shell
python -m build --wheel --outdir dist/local packages/ui
python -m pip install dist/local/dataindex_ui-0.1.0-py3-none-any.whl
dataindex-ui --model gpt-4o
```

pip downloads FastAPI and Uvicorn from PyPI. The command opens your browser and
prints a sign-in link that is valid for that run only. Use `--model llama3.1`
for a local server.

The UI listens only on this computer and is meant for **one user on one
computer**. It is not built for public hosting. Don't share the sign-in link.

- The UI and the CLI share one workspace. Uploads and chat history are saved
  there, including questions, answers and citation excerpts.
- Earlier messages are shown in the chat but are **not sent to the model**.
- Answers appear when they are complete; the progress messages are not
  token-by-token streaming.

See the [UI guide](packages/ui/README.md) for storage, deletion and server
options.

## Privacy, security and limits

- **Model traffic.** Chat sends your question, source names, outlines and
  bounded excerpts of retrieved evidence to the configured model provider.
  Indexing locally does not make remote chat private, so choose a provider your
  data policy allows.
- **Local data.** The workspace holds catalogs, evidence, database
  configuration, uploads and chat history, and these can contain sensitive
  content. DataIndex does not encrypt the workspace. Protect it with
  operating-system permissions and keep it out of version control.
- **Database credentials.** DataIndex never puts a password in a connection URL.
  It stores only the name of the `DATAINDEX_*` variable and reads the value when
  it opens a connection. On SQL Server, Snowflake, BigQuery and ClickHouse, a
  connection is only as read-only as the login it uses.
- **Plugins run as normal Python code.** In local mode, the first-party free
  readers are trusted by distribution name and version. Third-party and premium
  plugins must be explicitly allowlisted. Plugins run in the same process as
  DataIndex; discovery is not a sandbox, so verify where a package comes from
  before you install it.
- **Access checks.** The central dispatcher checks source authorization and
  license entitlement separately. A license never grants access to a source,
  and a tenant label on its own is not authentication.
- **Size limits.**
  - Text documents up to 5 MiB; binary documents up to 50 MiB.
  - At most 10,000 sections per document, and up to 256 KiB per retrieved
    section.
  - Chat questions up to 2,048 characters. Each question is answered on its
    own.

  The [documents guide](packages/extensions/documents/README.md) lists the
  limits for each format.
- **Not supported yet:**
  - OCR for scanned PDFs, and encrypted files whose keys are unavailable.
  - Hosted multi-user mode, process isolation for plugins, and an MCP server.
  - Production premium readers, and database engines not listed in the
    database guide.
  - BIM/CAD, subsurface and geospatial formats, which are planned for later
    releases.
- **Database server testing.** Live-server tests for PostgreSQL, MySQL,
  MariaDB, SQL Server, Oracle and MongoDB run in continuous integration. The
  experimental engines (Snowflake, BigQuery and ClickHouse) have no
  live-service tests.

## Documentation

| Guide | What it covers |
| --- | --- |
| [Python API](docs/PYTHON_API.md) | SDK reference: signatures, errors, budgets, citations |
| [Documents extension](packages/extensions/documents/README.md) | Supported formats, parsers, limits |
| [Databases extension](packages/extensions/databases/README.md) | Engines, connection URLs, views, query configuration, read-only guards |
| [Model adapter](packages/llm/README.md) | OpenAI-compatible and LiteLLM backends |
| [Browser UI](packages/ui/README.md) | Running the UI, storage and deletion |
| [Core package](packages/core/README.md) | Catalog, plugin runtime and CLI |
| [System architecture](docs/SYSTEM_ARCHITECTURE.md) | How the packages fit together |
| [Extension contract](docs/EXTENSION_CONTRACT.md) | Writing a reader plugin |
| [Security and entitlements](docs/SECURITY_AND_ENTITLEMENTS.md) | Authorization, trust and licensing checks for host applications |
| [Changelog](CHANGELOG.md) | Changes in this version |

## Contributing and support

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) before you
open a pull request.

Report security vulnerabilities privately, as described in
[SECURITY.md](SECURITY.md). Never include credentials, confidential documents or
private plugin code in issues. Use synthetic data in bug reports.

## License and attribution

The license for the DataIndex core and free packages has **not been chosen
yet**. Neither this repository nor wheels built from it grant any right to
redistribute. The repository includes a synthetic premium plugin that is used
only to test the shared extension contract. It contains no production premium
implementation or billing service.

The core includes a narrow adaptation of the hierarchy parent-assignment routine
from [PageIndex](https://github.com/VectifyAI/PageIndex), with its original MIT
notice preserved. [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) records the
pinned source and the changes made. DataIndex has **no runtime dependency on
PageIndex**.
