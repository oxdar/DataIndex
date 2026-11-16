# Independent packages, repositories, distribution and releases

**Status:** Core and Phases 02–06 have been built and tested in clean
environments, including a synthetic premium fixture. Phases 03–06 review gates
remain open, and the unresolved core/free-package license blocks redistribution.
A user-requested PageIndex-style local mode and the optional `dataindex-llm`
adapter are built on top (see [ADR-007](./adr/ADR-007-local-pageindex-style-mode.md)),
and so is the optional local browser chat `dataindex-ui`.
See [ADR-001](./adr/ADR-001-open-core-plugins.md) and
[ADR-006](./adr/ADR-006-repository-and-release-versioning.md).

## Public and private boundaries

The public `DataIndex` repository may develop the core and free extensions as a
monorepo, but each leaf builds and publishes a distinct wheel:

```text
packages/
  core/                         # dataindex-core; imports as dataindex
  extensions/
    documents/                  # dataindex-ext-documents
    databases/                  # dataindex-ext-databases
  llm/                          # dataindex-llm; imports as dataindex_llm
  ui/                           # dataindex-ui; imports as dataindex_ui
examples/
tests/
  contracts/
  integration/
```

Each package has its own `pyproject.toml`, source layout, tests, README,
changelog, license/notice and independent release version. No megawheel and no
unconditional optional-extension imports in core. `dataindex-llm` is not a
plugin: it is a separate adapter that supplies chat models by name, core imports
it lazily only when a model name is given, and core needs no provider SDK or
credential without it. `dataindex-ui` is not a plugin either: it is a local
FastAPI/Uvicorn chat page over `DataIndexClient`. Core never imports it, and it
is the only package that depends on FastAPI or Uvicorn.

Production commercial implementations stay in private repositories, outside the
public repository. The public repository includes only the synthetic
`tests/plugins/mock_premium` wheel for contract tests; it must not include
proprietary source. Private wheels depend on the public core API and follow the
same extension protocol.

## Install combinations

These commands become installable after the distributions are published; the
local wheel validation does not publish or authorize redistribution:

```powershell
python -m pip install dataindex-core
python -m pip install dataindex-ext-documents
python -m pip install dataindex-ext-databases
python -m pip install dataindex-llm
python -m pip install dataindex-ui        # optional browser chat; needs core and dataindex-llm
```

The documented one-line setup for the PageIndex-style local mode is:

```powershell
python -m pip install dataindex-core dataindex-ext-documents dataindex-ext-databases dataindex-llm
```

There is deliberately no meta-package and no `[all]` extra, because each
distribution must stay independently installable, versioned and removable.
`dataindex-llm[litellm]` adds the optional LiteLLM backend; the default
OpenAI-compatible backend uses only the standard library. Local-mode trust
defaults to the installed first-party free plugins; premium and third-party
plugins still need an explicit allowlist.

For proprietary extensions, use an authenticated private package feed or a
verified pinned wheel artifact. Do not use an unreviewed `--extra-index-url` path
that permits dependency-confusion. Verify artifact identity and hashes.

## Release and compatibility policy

Core distribution version, public extension API version and each extension
distribution version are separate. A plugin declares a PEP 440 `core_api_range`
in its static manifest; core rejects incompatible ranges before loading its
entry-point module. Breaking contract changes require a new API range. The API
starts at `0.1`; pre-1.0 compatibility is explicitly limited to declared ranges.
No package may claim compatibility merely because it installs. `dataindex-llm`
declares `dataindex-core>=0.1,<0.2` and is checked against the core chat-model
protocol by its own tests.

Distribution names remain proposed. A PyPI project JSON lookup returned HTTP 404
for `dataindex-core`, `dataindex-ext-documents` and `dataindex-ext-databases` on
2026-11-13, for `dataindex-llm` on 2026-11-14 and for `dataindex-ui` on
2026-11-15. This means no project
metadata was found at those endpoints then; it does **not** reserve the names or
prove they remain publishable. The bare name `dataindex` already exists on PyPI
as a yanked `0.0.0` placeholder whose owner is not identified, which is another
reason the core is proposed as `dataindex-core`. Recheck PyPI ownership/name
policy immediately before first publication.

## Required wheel-based CI matrix

`.github/workflows/release.yml` now builds all independent wheel/sdist
packages (core, documents, databases and `dataindex-llm`, plus the synthetic
premium wheel) and defines a Windows/Ubuntu, Python 3.11–3.13 validation matrix.
It does not publish packages. This workspace has no Git remote, so the workflow
has not yet run on GitHub.

| Install environment | Required checks |
|---|---|
| Clean venv + core wheel | Install, `pip check`, `import dataindex`, CLI/empty registry |
| Core + documents wheels without parser extras | Install, `pip check`, standard-library formats, graceful missing-parser hints |
| Core + documents wheel with `[all]` | Install, `pip check`, all format fixtures, optional-parser tests and local-client citations |
| Core + databases wheels | Install, `pip check`, bounded SQLite fixture operations with no driver extras |
| Core + databases wheel with `[duckdb,postgresql,mongodb]` and `mongomock` | Install, `pip check`, DuckDB file tests, the SQLAlchemy server path against a SQLite stand-in, MongoDB against mongomock; then remove the drivers and recheck SQLite |
| Core + databases wheel with server extras and live servers | PostgreSQL, MySQL, MariaDB, SQL Server, Oracle and MongoDB live contract tests; runs only in the `database-servers` job of `.github/workflows/databases.yml`, which fails if any live test is skipped |
| Core + both free wheels | Install, `pip check`, both extension suites/examples; remove databases and recheck documents/core |
| Core + both free wheels + `dataindex-llm` | Install, `pip check`, local-mode submit/chat against a loopback fake model; remove `dataindex-llm` and recheck that core-only tests and named-model errors still behave |
| Core + documents + `dataindex-llm` + `dataindex-ui` | Install, `pip check`, UI suite (sign-in, Host/Origin checks, saved history, SSE chat, uploads); runs in `.github/workflows/ui.yml`, not in `release.yml` |
| Core + synthetic premium wheel | Missing/grant/expiry/revoke/cross-tenant decisions, with free tools unaffected |
| Remove one extension | Core and every remaining plugin still function |

Every phase introducing a wheel must test installed artifacts in isolated
environments. Editable installs are useful for development but do not satisfy a
release gate. Track Python >=3.11, dependency licenses, package hashes and
provenance. No licensing network or LLM credential is needed for core-only use.

## Licensing and unresolved publication choices

- Core/free extension license (MIT or Apache-2.0) needs an explicit maintainer/legal
  decision; do not silently select one.
- The DocumentIndex wheel currently includes a pending-license notice rather
  than an approved grant. Local wheel installation is validation only and does
  not authorize redistribution.
- The DataBaseIndex wheel also carries a pending-license notice. SQLite source
  versions are host-supplied immutable snapshot identifiers; the extension
  detects ordinary main-database/WAL file-state changes but does not hash all
  database contents. Database server versions cover the reflected schema and
  approved queries, not row contents. Optional driver extras carry their own
  licenses (for example, psycopg is LGPL-3.0-only); see
  `THIRD_PARTY_NOTICES.md`.
- Any adapted PageIndex source must preserve Vectify AI's copyright and full MIT
  license notice, with exact upstream commit, source paths and transformations in
  `THIRD_PARTY_NOTICES.md`.
- Review all extension/transitive dependency licenses and trademarks separately.
- Commercial wheels use separate proprietary terms and controlled distribution.
- SBOMs, hashes, build provenance and signed attestations should be recorded where
  available; this is not a claim that signing infrastructure exists.
