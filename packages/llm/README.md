# dataindex-llm

The optional chat-model adapter for DataIndex. `dataindex-core` never imports a
provider SDK and never needs an API key. When you give it a model **name** such
as `"gpt-4o"`, core imports this package and calls
`dataindex_llm.create_chat_model(name, **options)`. The returned object implements
core's small `ChatModel` protocol; the reasoning planner uses it only to choose
retrieval tools and to cite evidence. Calculations and source reads stay
deterministic and run in DataIndex, not in the model.

This is not a DataIndex extension: it registers no `dataindex.extensions` entry
point and is found by its import name `dataindex_llm` only when a model name is
used. Passing your own object with a `complete()` method never loads it.

## Install

```powershell
python -m pip install dataindex-core
python -m pip install dataindex-llm                 # OpenAI-compatible, standard library only
python -m pip install "dataindex-llm[litellm]"      # optional LiteLLM backend
```

The default backend adds no dependency beyond `dataindex-core` and works on
Python 3.11 and newer.

## Quick start

Export your key; this package never reads `.env` files (see below).

```powershell
$env:OPENAI_API_KEY = "<your key>"
```

With the free extensions installed, this is the whole local workflow:

```python
from dataindex import DataIndexClient

client = DataIndexClient("gpt-4o")            # imports dataindex_llm on demand
doc_id = client.submit_document("./handbook.md")["doc_id"]
print(client.chat("What are the payment terms?", doc_id=doc_id))
```

The lower-level pieces are available when you build your own host:

```python
from dataindex.llm import ModelPlanner, resolve_chat_model

model = resolve_chat_model("gpt-4o")        # imports dataindex_llm on demand
planner = ModelPlanner(model)
```

`resolve_chat_model(name, backend)` accepts an optional `backend` mapping with
only `api_key` and `api_base`; core refers to it as `chat_backend`. You can also
call the factory directly:

```python
from dataindex_llm import create_chat_model

model = create_chat_model("gpt-4o", api_key="...", api_base="https://api.openai.com/v1")
```

## Which backend a model name selects

| Model string                        | Backend           | Name sent to the server          |
| ----------------------------------- | ----------------- | -------------------------------- |
| `gpt-4o`                            | OpenAI-compatible | `gpt-4o`                         |
| `openai/gpt-4o`                     | OpenAI-compatible | `gpt-4o` (one `openai/` removed) |
| `anthropic/claude-sonnet-4`         | OpenAI-compatible | unchanged                        |
| `meta-llama/Llama-3.1-8B-Instruct`  | OpenAI-compatible | unchanged                        |
| `litellm:anthropic/claude-sonnet-4` | LiteLLM           | `anthropic/claude-sonnet-4`      |

Prefixes other than `openai/` are kept because gateways such as OpenRouter,
vLLM and Ollama use them in their own model names. The `litellm:` prefix is
case-sensitive.

## OpenAI-compatible backend

`OpenAICompatibleChatModel` posts to `{base}/chat/completions` using only the
Python standard library. Configuration, highest priority first:

| Setting  | Explicit argument | Environment variable | Default                     |
| -------- | ----------------- | -------------------- | --------------------------- |
| API key  | `api_key`         | `OPENAI_API_KEY`     | none                        |
| Base URL | `api_base`        | `OPENAI_BASE_URL`    | `https://api.openai.com/v1` |

A key is required unless the server is on `localhost`, `127.0.0.1` or `::1`; with
no key on such a server no `Authorization` header is sent. The key is sent to
whichever base URL is configured, so check `OPENAI_BASE_URL` before exporting a
real key.

```python
create_chat_model("llama3.1", api_base="http://localhost:11434/v1")     # Ollama
create_chat_model("my-model", api_base="http://127.0.0.1:8000/v1")      # vLLM
create_chat_model("meta-llama/llama-3.1-8b-instruct",                   # OpenRouter
                  api_base="https://openrouter.ai/api/v1", api_key="...")
```

**No `.env` files.** The adapter reads only the process environment or the values
you pass. A `.env` file in a repository could silently redirect the base URL
and send your real key elsewhere, so export variables yourself or pass
`chat_backend={"api_key": ...}`.

### Security properties

- `https` is required, except plain `http` for `localhost`, `127.0.0.1` and
  `::1`. URLs with credentials (`user:pass@`), a query string or a fragment are
  rejected, and a rejected URL is never echoed back.
- Certificates are always verified with Python's default TLS context; there is no
  option to disable that.
- Redirects are never followed, so a gateway cannot bounce your `Authorization`
  header to another host. Loopback servers never go through an environment proxy;
  remote hosts use the proxy settings that Python's `urllib` honours.
- The key never appears in `repr()`, in attributes that are printed or in an
  error message. Error messages contain only the HTTP status, a short reason and
  the provider's own `error.message` (redacted, one line, at most 200
  characters). Request bodies, prompts, evidence, headers and non-JSON response
  text are never copied into an error.
- Responses are limited to 2 MiB (`max_response_bytes`), must be UTF-8 JSON
  without `NaN` or `Infinity`, and must carry a text `choices[0].message.content`.
- `timeout_seconds` sets an overall deadline for connecting, sending and reading
  the body, shared by any fallback retries (see Limitations for its precision).

### Request parameters

Requests use `temperature: 0`, `response_format: {"type": "json_object"}` when
JSON output is requested, and `max_completion_tokens` for `api.openai.com` or
`max_tokens` for every other host. When a server answers HTTP 400 or 422 and
names `response_format`, `temperature`, `max_tokens` or `max_completion_tokens`,
only the parameter that was named is dropped (or swapped between the two token
limits) and the request is repeated, at most three times and always within the
original deadline. Any other error status fails immediately.

## LiteLLM backend

`LiteLLMChatModel` is selected by a `litellm:` prefix and needs the
`dataindex-llm[litellm]` extra, which pins `litellm>=1.105.1,<1.106`. That is the
range PageIndex pins, and its pyproject notes that LiteLLM 1.107.0 was malware, so
widen the range only after reviewing a release. `litellm` is imported when the
model is created, never when `dataindex_llm` is imported. A missing package raises
`ChatModelConfigurationError` with the install command.

Calls use `drop_params=True` and `num_retries=0`, and `api_key` and `api_base`
are passed only when given; otherwise LiteLLM reads its own provider variables
such as `ANTHROPIC_API_KEY`. Failures are reported as
`LiteLLM request failed: <ExceptionClass>` (plus `status_code` when LiteLLM
provides one) without LiteLLM's message, which can contain request details.

LiteLLM is a large dependency with its own behaviour, and the URL and header rules
above apply only to the built-in backend. In particular, importing LiteLLM 1.105.1
calls python-dotenv and loads a `.env` file it finds, unless `LITELLM_MODE` is set
to something other than `DEV` (for example `PRODUCTION`). A stray `.env` file can
supply provider keys or base URLs that you did not set. To keep that from
happening, DataIndex sets `LITELLM_MODE=PRODUCTION` for the length of its own
`import litellm` when you have not set the variable yourself, and removes it
again afterwards. It cannot help if your process imported LiteLLM earlier or you
set `LITELLM_MODE=DEV` yourself; in that case set `LITELLM_MODE=PRODUCTION` in
the environment before starting the process. The tests exercise this backend with
a stand-in for `litellm.completion`, not with the real library.

## Limitations

- No streaming and no tool-calling API; planning uses strict JSON replies.
- No automatic retry on 429 or 5xx responses; callers decide whether to retry.
- Only OpenAI-style authentication is built in. Native Azure or Anthropic APIs
  need an OpenAI-compatible gateway or the LiteLLM backend.
- The deadline is checked between reads. One blocking operation such as DNS
  resolution or a stalled connection is bounded by the socket timeout, so a
  worst-case call can exceed `timeout_seconds`.
- Model instances are not guaranteed to be thread-safe; create one per thread.

## Data governance

Each planner call sends the question, source outlines, tool definitions and the
bounded evidence retrieved so far to the configured endpoint. Choose a provider
and base URL that your data policy allows, and use a self-hosted loopback or
private endpoint for confidential sources. This package does not authorize
source access or check license entitlement; DataIndex core does both before any
tool runs.

## Development

The tests use a local scripted HTTP server and fake keys; they need no network.
Until a core wheel that contains `dataindex.llm` is published, run them against the
working tree:

```powershell
$env:PYTHONPATH = "packages\core\src;packages\llm\src"
python -m pytest packages\llm\tests -q
```

## License status

The public core/free-extension license has not yet been approved. The included
[`LICENSE`](LICENSE) file explicitly marks this distribution as not licensed for
redistribution until maintainer/legal review selects a license. No release is
implied by local wheel builds.
