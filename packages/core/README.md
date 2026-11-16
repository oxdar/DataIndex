# dataindex-core

The DataIndex core package. It provides typed source/evidence models, a
versioned SQLite catalog, bounded hierarchy/graph navigation, a small
`DataIndexClient` SDK, and a local CLI for extension inspection, source listing,
document indexing and retrieval. A PageIndex-style local mode lets you submit
documents, SQLite and DuckDB files and database server connections, and ask
cited questions with
`DataIndexClient("gpt-4o").chat(...)` or `dataindex chat`; it needs the free
extensions and, for model names, the optional `dataindex-llm` package (see the
repository README and `docs/PYTHON_API.md`).

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
sources remain application responsibilities. See the repository's
`docs/EXTENSION_CONTRACT.md` and `docs/SECURITY_AND_ENTITLEMENTS.md` for the
design contract. `SQLiteCatalog` stores source revisions, typed nodes, evidence
and graph edges.
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
See `docs/PYTHON_API.md` in the repository for the full public surface.

The repository's core/free-extension license choice is still pending maintainer
review, so this initial build does not declare a distribution license.
