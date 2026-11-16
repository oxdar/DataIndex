# v0.1.0 release checklist

**Current state:** Phase 07 implementation is in progress. Local candidate
wheels/sdists and integration tests passed on Python 3.13.12, but the workflow
matrix has not run. The user-requested PageIndex-style local mode and the
`dataindex-llm` adapter (see [ADR-007](./adr/ADR-007-local-pageindex-style-mode.md))
add the items marked *local mode* below. Earlier review gates and license
decisions remain open; do not publish or tag `v0.1.0`.

- [ ] Phase 00-07 signoff records present in `docs/PHASE_STATUS.md`.
- [ ] PageIndex provenance audited and `THIRD_PARTY_NOTICES.md` updated if code copied.
- [ ] `dataindex-core` wheel/sdist builds and installs in clean venv with NO extensions.
- [ ] Free document extension builds and installs independently.
- [ ] Free SQL extension builds and installs independently.
- [ ] *Local mode:* `dataindex-llm` wheel/sdist builds and installs independently; core still imports and passes its own tests after it is removed; a model name without it fails at client creation with `ChatModelConfigurationError`.
- [ ] *Local UI:* `dataindex-ui` wheel/sdist builds and installs independently; its suite passes on installed wheels (`.github/workflows/ui.yml`); it binds only to loopback; chat history survives a restart; a human has reviewed the page in a real browser.
- [ ] Synthetic premium test wheel installs separately but cannot execute entitled tool without valid mock entitlement.
- [ ] Core runs without network or model keys; `import dataindex` passes.
- [ ] No runtime `pageindex`, vector DB or embedding deps.
- [ ] Indexed source structure reopens after process restart.
- [ ] Cited answers return actual locators and source versions; absent evidence yields abstention.
- [ ] *Local mode:* submit -> chat works end to end on installed wheels against a loopback fake model, returning citations with source ID, version, native locator and operation; the same files answered after a process restart.
- [ ] *Local mode:* only installed first-party free plugins are trusted by default; premium and third-party plugins need an explicit `trusted_plugins` entry; access is limited to submitted sources and entitlement is still checked separately.
- [ ] *Local mode:* prompt-injection tests pass for chat, agent tools and CLI output; no file path or API key reaches the model; model replies cannot override injected source IDs or versions.
- [ ] Read-only DB permissions tested against malicious operations.
- [ ] Trusted plugin allowlisting tested; incompatible extensions fail closed.
- [ ] Package matrix tests from `docs/ACCEPTANCE_CRITERIA.md` pass.
- [ ] CI builds wheels and executes isolated installs/tests.
- [ ] README quickstart and installation commands tested as written, including the local-mode Python and CLI examples.
- [ ] *Local mode:* README states the differences from PageIndex (no OCR, streaming, cloud mode or MCP; PDF needs an optional parser extra) and does not claim PyPI availability.
- [ ] License decision for DataIndex code approved; all third-party notices included.
- [ ] Package names verified for public publication (`dataindex-core`, the two extensions, `dataindex-llm` and `dataindex-ui`).
- [ ] Tag `v0.1.0` only after gates pass, publish release notes without unsupported claims.

The local-mode CLI (`submit`, `submit-db`, `documents`, `chat`) submits files,
SQLite and DuckDB database files, and database servers (by URL, with the
password in an environment variable) with approved typed queries. The older
catalog CLI remains document-only and has no SQL query configuration command.
Dependency/license audit, secret scan, SBOM/provenance, CI execution,
package-name recheck and license approval are still release blockers.
PostgreSQL, MySQL, MariaDB, SQL Server, Oracle and MongoDB are not live-verified
until the `database-servers` job in `.github/workflows/databases.yml` passes;
Snowflake, BigQuery and ClickHouse have no live tests and stay experimental.
