# dataindex-ui

A ready-to-use chat page for people who run DataIndex on their own computer.
`dataindex-ui` starts a small FastAPI + Uvicorn server on a loopback address and
serves one static HTML/JavaScript page. Every chat is saved, and the sidebar lists
the saved chats so you can reopen, rename or delete them. Answers show the same
citations as `DataIndexClient.chat()`: source, version, native locator, retrieval
operation and the cited excerpt.

This is an optional, separately installed package. It is not a DataIndex
extension: it registers no `dataindex.extensions` entry point and adds no
retrieval tools. It is a front end for the local `DataIndexClient`, and all
retrieval, authorization and citation checks still happen in `dataindex-core`.

## Install

```powershell
python -m pip install dataindex-core dataindex-llm dataindex-ui
python -m pip install dataindex-ext-documents      # to upload Markdown, text and HTML files
```

`dataindex-ui` depends on `dataindex-core`, `dataindex-llm`, `fastapi` and
`uvicorn`. It works on Python 3.11 and newer. Extensions stay optional; install
the ones whose sources you want to search.

## Run

```powershell
$env:OPENAI_API_KEY = "<your key>"
dataindex-ui --model gpt-4o
```

The command prints a sign-in link and opens it in your browser:

```text
DataIndex chat 0.1.0 is running. Open this link to sign in:
    http://127.0.0.1:8765/?token=...
Press Ctrl+C to stop.
```

| Option                 | Meaning                                                                 |
| ---------------------- | ----------------------------------------------------------------------- |
| `--storage DIR`        | Workspace directory (default `./.dataindex`), shared with `dataindex chat` |
| `--model NAME`         | Chat model; defaults to `$DATAINDEX_MODEL`. Model names work as in `dataindex-llm` |
| `--instructions TEXT`  | Extra guidance for the model, at most 4000 characters                   |
| `--host {127.0.0.1,::1}` | Loopback address to listen on (default `127.0.0.1`)                   |
| `--port N`             | Port (default 8765); `0` picks a free port                              |
| `--no-browser`         | Print the link without opening a browser                                |

Without a model the page still opens. You can then upload and remove documents
and read saved chats, but sending a question is disabled. A local
OpenAI-compatible server such as Ollama needs no key:

```powershell
$env:OPENAI_BASE_URL = "http://localhost:11434/v1"
dataindex-ui --model llama3.1
```

## Using the page

- **New chat** starts a conversation. It is saved when you send the first
  question, and the question becomes its title. Rename (✎) or delete (✕) a chat
  in the sidebar.
- **Documents** lists what is indexed in the workspace. **Upload** indexes
  every file type the documents extension reads: Markdown, text and HTML; PDF;
  Word, Excel and PowerPoint (`.docx`/`.xlsx`/`.pptx` and the 97-2003
  `.doc`/`.xls`/`.ppt`); OpenDocument; RTF; EPUB; email (`.eml`); and CSV, TSV,
  JSON, JSON Lines and XML. Text and data files may be up to 5 MiB; PDF, office,
  EPUB, RTF and email files up to 50 MiB. PDF and the 97-2003 Office formats
  need the documents extension's optional parsers:
  `pip install "dataindex-ext-documents[all]"`. Tick
  documents to limit the next question to them. Removing a
  document keeps saved chats, whose citations still record the source version.
- **Connect** indexes a database server through `dataindex-ext-databases`
  (PostgreSQL, MySQL/MariaDB, SQL Server, Oracle, Snowflake, BigQuery,
  ClickHouse or MongoDB). Enter a password-free connection
  URL, optionally a schema and a name, and paste the JSON of a
  `dataindex submit-db --config` file with the approved tables or views,
  columns and queries. The page has no password field: put the password in an
  environment variable named `DATAINDEX_...` before starting `dataindex-ui`,
  and enter only the variable's name. A URL that contains a password is
  refused. Install the driver extra for the engine, for example
  `pip install "dataindex-ext-databases[postgresql]"`.
- `[1]` markers in an answer link to citation cards. Abstentions and warnings
  are shown as DataIndex reports them.
- Each question is answered on its own. Earlier messages are shown and saved but
  are not sent to the model, as with `dataindex chat`.

## What is stored

Everything stays in the workspace directory:

| Path                     | Contents                                                        |
| ------------------------ | --------------------------------------------------------------- |
| `ui_history.sqlite3`     | Chats: titles, questions, answers with citations, errors, timestamps |
| `ui_uploads/`            | Copies of files uploaded through the page                       |
| everything else          | The normal DataIndex workspace (indexes and registries)         |

Answers are stored as `ChatAnswer.to_dict()` JSON, including citation excerpts.
Treat the history file with the same care as the documents themselves. On Linux
and macOS it is created with mode `600`. Deleting a chat removes its messages.
Removing an uploaded document also deletes its copy in `ui_uploads/`; documents
submitted from elsewhere are only removed from the index, never deleted from
disk. Limits: 500 chats in the sidebar, 1000 messages per chat, 2048 characters
per question.

## Security model

The page is for one person on one computer. It is not a hosted or multi-user
service.

- The server binds only to `127.0.0.1` or `::1`; there is no option to listen on
  another interface.
- Each run creates a random 256-bit token. Opening the printed link stores it in
  an `HttpOnly`, `SameSite=Strict` cookie and removes it from the address bar.
  Requests without the cookie get `401`. Anyone who can read the terminal output
  can sign in, so do not share the link.
- Requests must use a loopback `Host` header with the right port, which blocks
  DNS-rebinding pages. Requests that change data must also carry this page's
  `Origin`.
- A strict Content-Security-Policy allows only the page's own script and
  stylesheet. Document text, model output and saved history are always inserted
  as text, never as HTML.
- Rejected requests (`422`) report which field failed and why, never the value
  that was sent, so a mistyped secret is not echoed back.
- Uploaded file names are reduced to a safe base name and written only inside
  `ui_uploads/`. Questions run through the normal `DataIndexClient.chat()` path, so
  source authorization, license checks and citation validation are unchanged.
- The cookie is not tied to a port by browsers, and other software on the same
  computer that can read your files can read the history. Use operating-system
  accounts to separate people.

## Limitations

- Answers appear when they are complete. The stream sends progress events while
  DataIndex works, not tokens.
- One `DataIndexClient` serves the page from a single worker thread, so a
  running question delays document actions until it finishes. Chat history stays
  available.
- Uploads support the free documents extension's file types only. Connect
  database servers with **Connect**, and submit SQLite or DuckDB files with
  `dataindex submit-db`.
- The history schema is versioned. A newer file opened by an older
  `dataindex-ui` is refused rather than changed.

## Development

```powershell
python -m pip install build
python -m build --wheel --outdir dist packages\core
python -m build --wheel --outdir dist packages\extensions\documents
python -m build --wheel --outdir dist packages\llm
python -m build --wheel --outdir dist packages\ui
python -m venv .venv-ui
.venv-ui\Scripts\python -m pip install pytest httpx (Get-ChildItem dist\*.whl).FullName
.venv-ui\Scripts\python -m pytest packages\ui\tests -q
```

Install the extension from a wheel, as above. An editable install of
`dataindex-ext-documents` cannot find its manifest, and the upload tests then
fail. The tests use a scripted chat model and need no network or key.

## License status

The public core/free-extension license has not yet been approved. The included
[`LICENSE`](LICENSE) file explicitly marks this distribution as not licensed for
redistribution until maintainer/legal review selects a license. No release is
implied by local wheel builds.
