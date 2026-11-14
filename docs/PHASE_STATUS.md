# DataIndex v0.1.0 phase status

**Updated:** 2026-11-14  
**Repository state:** Phases 03–06 are implemented and locally validated.
Phase 07 SDK/CLI and release-readiness work is in progress, and a user-requested
PageIndex-style local mode (client, CLI, `dataindex-llm` adapter) was added on
top of it; see the local-mode addendum at the end of this file. Phases 03–06
review gates remain open, and required free-package license decisions are
unresolved. Nothing is tagged or published. No Git repository was present in the
inspected workspace, so no commit is recorded.

| Phase | Status | Build evidence | Approval |
|---|---|---|---|
| 00 Architecture | COMPLETE — documentation gate | ADR-001–006, refined contracts/diagrams, source audit and package lookup | User directed start of implementation |
| 01 Core plugin packaging | READY FOR REVIEW | 16 tests pass on Python 3.11.6, 3.12.13 and 3.13.12; built wheel installs cleanly and `pip check` passes | Pending human review |
| 02 Typed indexes | READY FOR REVIEW | 27 core tests pass on Python 3.11.6; clean wheel install, catalog smoke test and `pip check` pass | Pending human review |
| 03 Free documents | IMPLEMENTED — VALIDATED; REVIEW/LICENSING GATE OPEN | Core and documents wheels build/install; 29 core tests and 9 document tests pass; uninstall isolation verified | Pending |
| 04 Free SQL | IMPLEMENTED — VALIDATED; REVIEW/LICENSING GATE OPEN | SQLite wheel, 15 tests, example, dependency and uninstall checks pass on Python 3.11.6 | Pending human review |
| 05 Reasoning | IMPLEMENTED — VALIDATED; REVIEW GATE OPEN | Core and extension wheels build/install; 74 mixed-install tests pass, 46 core-only tests pass; mixed-source demo and removal checks pass | Pending human review |
| 06 Premium contract test | IMPLEMENTED — VALIDATED; REVIEW GATE OPEN | Core, documents, databases and synthetic premium wheels pass isolated mixed install, entitlement denial/grant/expiry/revoke/cross-tenant and core-removal checks | Pending human review |
| 07 v0.1 release gate | IN PROGRESS — CANDIDATE NOT RELEASED | See the Phase 07 progress record and the local-mode addendum below; five wheels, mixed suite 2675 passed on Python 3.11.15, 3.12.13 and 3.13.12 | Pending |

## Phase 00 acceptance record

- **Architecture and responsibilities:** documented in `docs/SYSTEM_ARCHITECTURE.md`,
  including indexing/evidence, retrieval, installation and commercial activation
  diagrams.
- **Greenfield boundary:** workspace inspection found documentation, prompts and
  workspace metadata only; no Python implementation or tests were assumed.
- **Package boundaries and contract:** described in ADR-001/006,
  `docs/REPOSITORY_AND_PACKAGING.md` and `docs/EXTENSION_CONTRACT.md`.
- **Security boundary:** trust, source authorization, entitlement, tenant scope
  and resource limits are separate in ADR-004 and
  `docs/SECURITY_AND_ENTITLEMENTS.md`.
- **PageIndex provenance:** pinned commit and inspected source/license are
  recorded in ADR-003 and `docs/PAGEINDEX_SOURCE_STRATEGY.md`; no code was copied
  in this documentation-only phase.
- **Install/test matrix:** documented for core-only, each free extension, mixed
  sources, premium deny/grant/revoke and extension removal. No wheel existed at
  the Phase 00 gate.
- **Validation:** no automated tests or builds were run; this phase changed
  documentation only and the repository contains no documentation test runner.
  Upstream source inspection and PyPI lookup were completed; the PyPI 404 results
  are not proof that names can be claimed.

## Decisions and open items before implementation/publication

- All six ADRs are proposals pending maintainer review; do not treat them as
  accepted until approved.
- Core/free package license (MIT or Apache-2.0) remains undecided.
- PyPI names `dataindex-core`, `dataindex-ext-documents` and
  `dataindex-ext-databases` returned HTTP 404 on 2026-11-13; recheck name and
  ownership rules immediately before publication.
- Phase 01 validated a narrow PageIndex tree-parenting adaptation and included
  the full MIT notice in both the repository notice and core wheel.
- Authentication/tenant identity is a local/offline prototype boundary in v0.1;
  real hosted identity and isolation are out of scope.

## Phase 01 acceptance record

- **Core package:** built `dataindex_core-0.1.0-py3-none-any.whl`; installed it
  into a clean Python 3.11.6 virtual environment. `pip check` reported
  `No broken requirements found`; `import dataindex` reported `0.1.0`.
- **Core-only behavior:** `python -m dataindex extensions list` printed
  `No extensions discovered.` The import test blocks `dataindex_ext_*` imports
  and verifies the packaged PageIndex notice exists.
- **Registry:** tests cover zero plugins, safe metadata-only discovery, explicit
  trusted/enabled loading, duplicate IDs, incompatible API, untrusted plugins
  and isolated plugin-load failure.
- **Test matrix:** `python -m pytest` passed all 16 tests on Python 3.11.6,
  3.12.13 and 3.13.12 (48 passing test executions total). Tests used
  `PYTHONPATH=packages/core/src`.
- **Build/install commands:** `python -m build --wheel --outdir dist-verified`,
  `python -m pip install <built-wheel>`, `python -m pip check`,
  `python -c "import dataindex"`, and `python -m dataindex extensions list`
  passed in an isolated wheel environment.
- **Source/licensing:** no `pageindex` runtime dependency; the adapted helper's
  exact upstream commit, transformation and full MIT license are recorded in
  root and packaged `THIRD_PARTY_NOTICES.md`.
- **Limits/open issues:** no persistent store, extension reader, retrieval
  dispatcher, entitlement service or LLM is implemented in this phase. The
  DataIndex core/free-package license remains undecided, so publication is not
  ready. The current trust allowlist matches distribution name/version only;
  artifact signature/hash verification is the installer's responsibility. No
  linter/type-checker is configured; Pylance syntax checks passed. Workspace
  diagnostics still show unresolved imports because the selected VS Code
  interpreter is not configured for the core package/test environment.
- **Validation note:** a local Python 3.13 editable-install attempt failed while
  writing generated egg-info. The source test suite then passed on all three
  runtimes via `PYTHONPATH`, and the final wheel was built from a clean temporary
  source copy and passed isolated installation checks.

Phase 01 was ready for review at its phase gate. The user explicitly instructed
progression to Phase 02; Phase 01 is not being retroactively marked approved.

## Phase 02 acceptance record

- **Architecture decisions:** `SQLiteCatalog` uses the Python standard-library
  SQLite driver, schema version 1 and explicit transactions. Source revision
  metadata is retained; moving a source to a new revision atomically removes its
  previous nodes, evidence and graph edges so stale records cannot be presented
  as current. Unknown future schema versions fail explicitly.
- **Catalog and navigation:** source and node create/update/remove operations,
  unique node IDs, same-source-revision parent constraints, cycle checks,
  deterministic keyword search, child listing and bounded breadth-first graph
  traversal are implemented. Search/traversal results cap at 1,000 and depth
  caps at 16.
- **Evidence and graph provenance:** evidence is stored only after the supplied
  `LocatorDereferencer` resolves its native locator. Observed values must equal
  the value read from the authoritative source; evidence values are limited to
  64 KiB. Relationships require persisted evidence from an endpoint source,
  preserve their `observed`/`calculated`/`model_inferred` type and optional
  identity confidence, and reject evidence-kind mismatches.
- **Tenant boundary:** source records carry a tenant label and graph links
  between different tenant labels are rejected. This is not tenant
  authorization: the catalog is a local storage layer and has no access-policy
  or dispatcher enforcement.
- **Acceptance results:**
  - Create, close, reopen, navigate and cite the same source revision: **PASS**
    (`test_persistent_catalog_reopens_hierarchy_graph_and_verified_evidence`).
  - Duplicate node IDs, missing/cross-revision parents, cycles, stale versions,
    and cross-tenant graph links rejected: **PASS**.
  - Unresolvable/fabricated locators, mismatched observed values, missing
    evidence references, and evidence-kind upgrades rejected: **PASS**.
  - Core-only tests with no extension or LLM configured: **PASS**.
  - Revision metadata retained and old snapshot invalidated atomically:
    **PASS**.
- **Test command and result:** with Python 3.11.6 in an isolated validation
  environment and `PYTHONPATH=packages\core\src`,
  `python -m pytest packages\core\tests -q` → **27 passed**. An initial run had
  one assertion-regex mismatch (the implementation's error said “resolve”);
  the test was corrected and the complete suite rerun successfully.
- **Wheel/install evidence:** built
  `dataindex_core-0.1.0-py3-none-any.whl` from a clean temporary source copy
  with `python -m build --wheel`. Installed it into a separate clean Python
  3.11.6 environment; `pip check` reported `No broken requirements found`;
  import reported version `0.1.0`, schema `1`, and no optional plugin imports;
  `python -m dataindex extensions list` printed `No extensions discovered.`
- **Static checks:** Pylance file syntax checks passed for the new modules and
  catalog tests; Pylance reported no diagnostics in `catalog.py`,
  `locators.py` or `__init__.py`. Pylance still reports unresolved
  `packaging`/`pytest` and test src-layout imports in the selected editor
  environment; the isolated test and wheel environments resolve them. No
  project linter or static type checker is configured.
- **Limits and risks:** no format parser, native source reader, FTS index,
  retrieval dispatcher, source authorization, or LLM is implemented. Historical
  source revision metadata is retained, but historical node/evidence snapshots
  are not. Calculated and model-inferred evidence only proves locator
  resolution; it does not independently verify calculations or inferences.
  SQLite evidence values are not encrypted by core. Tenant labels only prevent
  cross-tenant graph links; reads are not tenant-authorized, so do not share a
  catalog file across trust boundaries without external access controls.

Phase 02 was ready for human review at its phase gate. The user explicitly
instructed progression to Phase 03; Phase 02 is not retroactively marked
approved.

## Phase 03 acceptance record

- **Independent extension:** `packages/extensions/documents` builds
  `dataindex_ext_documents-0.1.0-py3-none-any.whl` with the
  `dataindex.extensions` entry point `documents`. Its packaged static manifest
  is discovered from installed package files without importing plugin code.
- **Natural structure and locators:** Markdown headings (including nesting),
  plain-text paragraphs and HTML headings are indexed using native line,
  heading, anchor and paragraph locators. Retrieval ranks section-local text
  using whole-word matches and returns the selected section as observed
  source-backed evidence. HTML active-content elements are excluded.
- **Source and bounds:** the host passes authorized source bytes or UTF-8 text
  explicitly; the extension does not resolve source IDs as paths and performs
  no filesystem/network reads. Input is limited to 5 MiB, 100,000 lines/events
  and 10,000 sections; queries are limited to 256 characters. Unsupported
  formats, invalid encodings, empty input, revision conflicts and expired
  deadlines fail explicitly.
- **Acceptance results:**
  - Markdown/TXT/HTML fixtures produce natural sections and native locators:
    **PASS**.
  - Specific HTML child section wins when its direct text matches the query;
    hidden active content is not indexed, and substring-only matches do not
    produce false hits: **PASS**.
  - Retrieved evidence carries source ID, version, native locator and
    `documents.search` operation, and the Phase 02 catalog verifies the
    observed value: **PASS**.
  - Core-only wheel installs and imports with no extension discovered:
    **PASS**.
  - Document wheel discovers metadata without importing the plugin, then loads
    only when trusted and enabled: **PASS**.
  - Uninstalling the document wheel leaves core usable and the registry empty:
    **PASS**.
- **Build/test/install evidence:** built both wheels from clean staged source
  copies with `python -m build --wheel --outdir <temporary-wheel-dir>`. In clean
  Python 3.11.6 environments, `pip check` reported `No broken requirements
  found`; `python -m pytest packages\core\tests -q -p no:cacheprovider` passed
  **29 tests**, and
  `python -m pytest packages\extensions\documents\tests -q -p no:cacheprovider`
  passed **9 tests**. The basic usage example ran successfully and printed
  source ID, version, locator, retrieval operation and evidence. After
  `python -m pip uninstall -y dataindex-ext-documents`, `pip check` still
  passed and `python -m dataindex extensions list` printed
  `No extensions discovered.`.
- **Syntax/static checks:** Pylance syntax checks passed for `readers.py`,
  `plugin.py`, `registry.py` and `test_registry.py`. No project linter or static
  type checker is configured. Pylance diagnostics still report unresolved
  project/package imports because the selected editor interpreter is not
  configured with these source paths and test dependencies; the isolated wheel
  and test environments resolved them. Pylance also reports existing unused
  local/parameter warnings in the touched plugin/test files.
- **Implementation fixes included in validation:** child-specific queries are
  ranked using each section's direct text while the full section remains
  citation evidence. The core registry now safely falls back to distribution
  package files when metadata `read_text()` cannot find package data, and
  rejects invalid module paths before looking up a manifest.
- **Open gate and limits:** the extension's `LICENSE` is an explicit pending
  license notice, not an approved license grant. The core/free-package license
  still requires maintainer/legal selection; do not redistribute either
  package until approved. HTML parsing is not browser rendering or a
  sanitization service. PDF/OCR, filesystem watching, persistence of registered
  document bytes and multi-result retrieval are not implemented. The user
  explicitly directed progression to Phase 04 while this gate remains open;
  that progression does not retroactively approve Phase 03.

## Phase 04 acceptance record

- **Independent extension:** `packages/extensions/databases` builds
  `dataindex_ext_databases-0.1.0-py3-none-any.whl` with the
  `dataindex.extensions` entry point `databases`. It depends only on
  `dataindex-core` and Python's standard-library `sqlite3` at runtime.
- **Schema and query model:** only explicitly allowlisted tables and columns
  are indexed. Primary keys are collected from the full schema for correct
  ordering validation; unallowlisted PK names are not emitted as table
  metadata. Fully allowlisted FK constraints become relationship nodes.
  Composite-FK joins are rejected because the typed query contract currently
  supports one join pair per joined table.
- **Query controls:** requests select an approved query ID and pass exact
  declared filter values; they cannot provide SQL, identifiers, operators or
  joins. Identifiers are quoted, values are bound, filters have payload and
  SQLite-compatible parameter caps, and `IN` values cannot contain `null`.
  Supported aggregates are `COUNT` and `SUM`. Non-aggregate results are
  ordered by every joined table's complete primary key; grouped aggregates use
  their group columns. Results are limited to 100 rows, 32 selected columns and
  64 KiB of serialized row data.
- **Read-only and source consistency:** connections use URI `mode=ro`,
  `query_only`, `trusted_schema=OFF`, a deny-by-default SQLite authorizer,
  bounded VM instructions and a cooperative deadline. Registration and queries
  use explicit SQLite read transactions. The host must provide an immutable
  snapshot identifier as `SourceRef.version`; a SHA-256 digest of main-file and
  WAL identity metadata (device, inode, size and modification time) detects
  ordinary file changes, but is not a content hash.
- **Evidence:** query evidence carries source ID/version, query ID, canonical
  request filters, effective limit, query-definition digest, file-state digest,
  UTC read timestamp, schema version, result rows/count/truncation and
  `databases.query` operation. Aggregates are marked `CALCULATED`; row results
  are `OBSERVED`. Earlier registered revisions are retained up to a 256-revision
  limit.
- **Acceptance results:**
  - Allowlisted schema, full/composite PK handling, FK nodes and rejection of
    partial composite-FK joins: **PASS**.
  - Live filtered rows, approved FK join, deterministic `SUM`/`COUNT`, evidence
    provenance and tenant fixed-filter behavior: **PASS**.
  - Arbitrary SQL input, DML/DDL, unauthorized columns and non-FK joins:
    **REJECTED**.
  - Filter/row/VM/deadline bounds, oversized result rejection, schema and file
    changes, output alias collisions, NULL `IN` values and source revision
    retention: **PASS**.
  - Installed entry-point discovery requires explicit trust and enablement:
    **PASS**.
  - Removing the database wheel leaves the documents suite and example
    functional; removing documents afterward leaves core-only tests/import and
    an empty registry functional: **PASS**.
- **Build/test/install evidence:** using a Python 3.11.6 validation interpreter
  (`$python`) and temporary wheel output directory (`$out`), built the wheels
  with `& $python -m build --wheel --outdir "$out\core" packages\core`,
  `& $python -m build --wheel --outdir "$out\documents"
  packages\extensions\documents`, and
  `& $python -m build --wheel --outdir "$out\databases"
  packages\extensions\databases`. Installed them into a fresh venv with
  `& $python -m pip install pytest "$out\core\dataindex_core-0.1.0-py3-none-any.whl"
  "$out\documents\dataindex_ext_documents-0.1.0-py3-none-any.whl"
  "$out\databases\dataindex_ext_databases-0.1.0-py3-none-any.whl"`.
  `& $python -m pip check` reported `No broken requirements found`. The
  installed database test command
  `& $python -m pytest packages\extensions\databases\tests -q -p no:cacheprovider`
  passed **15 tests**, and
  `& $python -m pytest packages\extensions\documents\tests -q -p no:cacheprovider`
  passed **9 tests**. `& $python packages\extensions\databases\examples\basic_usage.py`
  printed deterministic totals of **150000** and **90000 USD cents**. After
  `& $python -m pip uninstall -y dataindex-ext-databases`, the document suite
  passed **9 tests** and
  `& $python packages\extensions\documents\examples\basic_usage.py` ran.
  After uninstalling documents, the core suite command
  `& $python -m pytest packages\core\tests -q -p no:cacheprovider` passed
  **29 tests**, `& $python -m dataindex extensions list` printed
  `No extensions discovered.`, and the core-only import check passed.
- **Static checks:** Pylance syntax checks passed for the database source,
  exception/export modules and tests. The selected editor environment still
  reports unresolved imports for the workspace source packages and `pytest`;
  installed-wheel test environments resolve them. No project linter or static
  type checker is configured. Local runtime validation used Python 3.11.6; the
  workflow defines Python 3.11–3.13 but those other versions were not run here.
- **Limits/open gate:** local SQLite files only; no PostgreSQL, views, virtual
  tables, arbitrary SQL, pagination, or composite-FK joins. Source authorization
  and protected file access remain host responsibilities; fixed scope filters
  do not replace the central dispatcher or OS permissions. The file-state
  digest does not prove byte-for-byte immutability. The extension's LICENSE is
  a pending notice, not an approved grant; redistribution remains blocked.

Phase 04 is ready for human review but is not approved. Phase 03's license and
review gate also remains open; progression to Phase 04 did not retroactively
approve Phase 03. The user explicitly requested Phase 05 while the earlier
review gates remained open; that request does not retroactively approve Phases
03 or 04.

## Phase 05 acceptance record

- **Core API and independence:** added `dataindex.reasoning` with a
  provider-neutral `LLMPlanner` protocol, bounded `ReasoningEngine`,
  hierarchical metadata source selection, deterministic execution, structured
  planner turns, citation-backed claims and abstention. Core imports no LLM
  provider or extension package; host identity and policy dependencies are
  typed source-authorization and entitlement interfaces.
- **Source and tool policy:** a source is offered only after catalog tenant
  ownership and host `source.access` authorization pass. Tool capabilities and
  commercial entitlements are checked independently. The dispatcher checks the
  loaded extension's trust, compatibility, enablement and current health, then
  rechecks source/version, capability, entitlement, request schema and result
  provenance immediately before returning evidence.
- **Bounded execution:** defaults cap depth at 3, calls at 8, model-reported
  tokens at 8,000, results at 100 and elapsed time at 30 seconds; configured
  limits cannot exceed depth 8, calls 32, tokens 128,000, results 100 or
  300 seconds. Source planning inspects at most 64 authorized sources and 256
  catalog nodes per source. Evidence is capped at 256 KiB per tool result.
  Tool schemas use a 32 KiB, depth-16 supported JSON Schema subset and fail
  closed on unsupported keywords. Deadlines are cooperative; core cannot
  terminate a blocked in-process plugin or planner.
- **Source-aware tools:** documents and SQLite extensions implement optional
  `tools_for_source()` discovery and return no tools for unregistered revisions.
  SQLite publishes only that source's approved query IDs in its input schema;
  SQL remains a predeclared typed query and never model-authored text.
- **Evidence and answers:** tool evidence must match the selected source ID,
  revision and operation; catalog-backed locators must equal their stored node
  locator. Claims must cite evidence execution IDs and unknown citations fail.
  Non-catalog evidence locators receive structural/provenance checks only,
  because the database extension does not expose a generic dereferencer.
  Citation presence is checked, not natural-language entailment; applications
  must not treat a citation as proof that generated wording is semantically
  supported.
- **Acceptance results:** deterministic no-model mixed-source execution:
  **PASS**. Synthetic planner retrieved the Markdown payment section and an
  approved SQLite aggregate, reporting **150000** and **90000 USD cents** and
  **net 30 days after receipt**, with both native locators and operations:
  **PASS**.   no-source/cross-tenant abstention, missing capability authorization,
  missing entitlement, untrusted tool selection, depth/call/token/deadline
  budgets, invalid schemas and request properties, hallucinated citation IDs
  and mismatched catalog locators: **PASS**. The prompt-injection fixture's
  attempted `sql`/`DROP TABLE` request was rejected before the SQLite extension
  ran: **PASS**.
- **Wheel/build/install checks:** built
  `dataindex_core-0.1.0-py3-none-any.whl`,
  `dataindex_ext_documents-0.1.0-py3-none-any.whl`, and
  `dataindex_ext_databases-0.1.0-py3-none-any.whl` with Python 3.11.0 and
  installed them into a fresh venv. `python -m pip check` reported
  `No broken requirements found`. The mixed-install command
  `python -m pytest packages\core\tests packages\extensions\documents\tests
  packages\extensions\databases\tests tests\integration -q -p
  no:cacheprovider -k 'not test_extensions_list_works_without_plugins'`
  passed **74 tests** (one core-only CLI test was intentionally deselected
  because both plugins were installed). The document and database basic
  examples and `python examples\mixed_sources.py` all ran; the latter printed
  both totals and source citations.
- **Removal isolation:** after uninstalling the database wheel, `pip check`
  passed, the document suite passed **10 tests**, and its basic example ran.
  After uninstalling documents, `pip check` passed, the core suite passed
  **46 tests**, `import dataindex` reported `0.1.0` without loading any
  `dataindex_ext_*` modules, and `python -m dataindex extensions list` printed
  `No extensions discovered.`.
- **Static checks and limits:** Pylance syntax checks passed for the changed
  Python files; no project linter or static type checker is configured. The
  selected Pylance interpreter does not include the workspace package paths,
  so it reports unresolved imports in the example/tests; isolated wheel tests
  resolved and exercised those imports. Entitlement policy is a host-supplied
  interface, not billing infrastructure. Hosted authentication,
  server-side policy deployment, audit logging, persistent quotas, plugin
  sandboxing and semantic entailment checks are not implemented. The Phase 03
  and Phase 04 review/license gates remain open; public redistribution remains
  blocked pending an approved license.

## Phase 06 acceptance record

- **Progression:** Phase 05 remains ready for review and is not approved. The
  user explicitly requested Phase 06 on 2026-11-14; this progression does not
  retroactively approve Phase 05 or the open Phase 03/04 review and licensing
  gates.
- **Provider contract:** added `EntitlementProvider.check(tenant_id,
  capability, deployment) -> EntitlementDecision`. The `capability` argument is
  the entitlement ID mapped from the extension's declared tool capability.
  Decisions use stable reason codes for grant, missing, expired, revoked,
  tenant/deployment mismatch and provider-unavailable states. `ToolDispatcher`
  requires a deployment identifier, checks commercial entitlements on every
  dispatch, fails closed when the provider is absent, logs the denial reason
  code, and carries it on `EntitlementDeniedError`. Free tools do not call the
  entitlement provider.
- **Independent fixture:** created
  `tests/plugins/mock_premium`, an independently buildable
  `dataindex-ext-mock-premium` wheel with its own packaging metadata, entry
  point, static manifest, synthetic implementation, tests, README, changelog,
  constraints, pending-license notice and third-party notice. It contains no
  proprietary feature, billing integration or provider secret. The in-memory
  entitlement provider and its decision-event log exist only in the contract
  tests.
- **Acceptance tests:** installed metadata reports the mock premium wheel while
  loading it only after trust/compatibility/enablement. Calls fail without a
  provider or grant and do not execute the plugin; an authorized grant returns
  evidence with source/version/locator/operation; expiry and revocation block
  execution; cross-tenant source access is denied before entitlement checking.
  After revocation, both the free DocumentIndex and SQLite tools still execute
  successfully, including a fixed tenant filter on the SQLite fixture:
  **PASS**.
- **Wheel/build/install checks:** built
  `dataindex_core-0.1.0-py3-none-any.whl`,
  `dataindex_ext_documents-0.1.0-py3-none-any.whl`,
  `dataindex_ext_databases-0.1.0-py3-none-any.whl`, and
  `dataindex_ext_mock_premium-0.1.0-py3-none-any.whl` with Python 3.13.12.
  Installed all four wheels in a clean venv; `python -m pip check` reported
  `No broken requirements found`. The mixed-install command
  `python -m pytest packages\core\tests packages\extensions\documents\tests
  packages\extensions\databases\tests tests\plugins\mock_premium\tests -q -p
  no:cacheprovider -k 'not test_extensions_list_works_without_plugins'`
  passed **76 tests** (one core-only test was deselected with plugins installed).
  `python examples\mixed_sources.py` printed the expected **150000** and
  **90000 USD cents** and the **net 30 days after receipt** statement, with
  Markdown and SQLite evidence locators.
- **Removal isolation:** uninstalled all three extension wheels from the same
  environment. `python -m pip check` passed, the core suite passed **46 tests**,
  `import dataindex` loaded no `dataindex_ext_*` modules, and
  `python -m dataindex extensions list` printed `No extensions discovered.`.
- **Static checks and limits:** Python syntax checks passed for the changed core,
  fixture, example and test files. Pylance reports no diagnostics in core
  entitlement/dispatcher modules; its selected interpreter does not include the
  workspace package paths, so it reports unresolved imports in the fixture/tests.
  No project linter or static type checker is configured. This is not a
  production entitlement service, durable audit store, signed-license verifier,
  billing system or plugin sandbox. The fixture's package license and the
  core/free-package license remain pending; wheel builds are validation only,
  and redistribution remains blocked.

Phase 06 is ready for human review but is not approved. The user explicitly
requested progression to Phase 07 on 2026-11-14; that request does not
retroactively approve Phase 06 or the open Phase 03–05 review gates.

## Phase 07 progress record

- **Progression and release status:** Phase 06 acceptance tests had passed,
  but its human review remains open. The user explicitly requested Phase 07;
  work proceeds without recording approval of Phase 03–06. This is an
  unreleased candidate only. No tag or publication was created.
- **SDK:** exported `DataIndexClient` provides typed source registration,
  bounded extension indexing, tenant- and capability-authorized source listing
  and metadata search, central-dispatch retrieval, planner-backed `ask`, and
  extension status. Catalog node batches are transactional and idempotent.
  Plugins are discovered by metadata, then loaded only when exact
  distribution/version trust and enablement are supplied. Hosts provide source
  authorization, deployment identity and source bytes/configuration.
- **CLI:** supports extension list/inspect, tenant-filtered source listing,
  bounded node metadata search, and local document index/query for Markdown,
  plain text and HTML files up to 5 MiB. Index/query require explicit
  distribution/version trust and `--enable-plugin`; content versions use
  SHA-256, stale files are rejected, and query returns a source/version/native
  locator/operation. It does not expose arbitrary SQL or a database query
  configuration interface. Tenant labels are not authentication; do not share
  catalogs across trust boundaries.
- **Documentation and release metadata:** added a Python API reference,
  documented/tested README CLI quickstart, contributor and security guidance,
  draft changelog/release notes, a root `.gitignore`, and a non-publishing
  release-validation workflow for Windows/Ubuntu and Python 3.11–3.13.
- **Build/install evidence:** with Python 3.13.12, built the wheel and sdist for
  `dataindex-core`, `dataindex-ext-documents`, `dataindex-ext-databases`, and
  the synthetic `dataindex-ext-mock-premium` into `dist\phase07-final`.
  Installed the four wheels in a clean temporary venv with local wheel lookup;
  `pip check` passed after core and each extension installation.
- **Mixed validation:** command:
  `python -m pytest packages\core\tests packages\extensions\documents\tests
  packages\extensions\databases\tests tests\plugins\mock_premium\tests
  tests\integration -q -p no:cacheprovider -k
  'not test_extensions_list_works_without_plugins'` using the wheel-installed
  Python 3.13.12 environment → **88 passed, 1 intentionally deselected**. The
  mixed-source example produced **150000** and **90000 USD cents**, with cited
  Markdown and SQLite evidence.
- **Core-only validation:** after uninstalling all three extension wheels in a
  temporary venv, `pip check` passed, core tests passed **51**, import reported
  `0.1.0` without loading any `dataindex_ext_*` modules, and
  `python -m dataindex extensions list` printed `No extensions discovered.`.
- **CLI quickstart validation:** ran the README `index`, `query`, `sources
  list`, and `extensions inspect` commands against `README.md` and a temporary
  catalog. Indexing produced **7** records; retrieval returned the `Product`
  Markdown heading with source hash, native locator and `documents.search`
  operation. Metadata inspection correctly reported `trusted=false` and
  `enabled=false` because that command does not load or trust plugin code.
  Integration tests reject missing enablement, an untrusted version and
  changed file content.
- **Static checks:** Pylance syntax checks passed for the changed Python
  modules/tests. No diagnostics were reported in `sdk.py`; the selected
  Pylance environment reports the optional `dataindex_ext_documents` import in
  `cli.py` unresolved because the extension is not installed there. The
  wheel-based CLI/integration tests resolve and exercise it. No repository
  linter or static type-checker is configured.
- **Pending release acceptance:** the new GitHub workflow was not run because
  this workspace has no Git repository/remote. The Python/OS CI matrix,
  dependency/license audit, secret scan, SBOM/provenance, and PyPI name/ownership
  recheck remain unverified. Core/free and extension license decisions are
  unresolved, and Phase 03–06 human reviews remain open. Those are release
  blockers; local wheel builds do not grant redistribution rights.

Phase 07 is in progress and blocked from release. Stop here for human review;
do not publish or tag `v0.1.0`.

## Local-mode (PageIndex-style) addendum

After the Phase 07 progress record above, the user asked for DataIndex setup and
usage to be "very similar to PageIndex". This is a scope addition on top of the
Phase 07 candidate, not part of the original phase plan.
[ADR-007](./adr/ADR-007-local-pageindex-style-mode.md) is **Proposed** and awaits
maintainer review. The counts in the Phase 07 record above (four wheels, 88 mixed
tests, 51 core-only tests) predate this work and the figures below supersede
them. The catalog commands described there (`index`, `query`, `sources`,
`extensions`) remain available; the local commands are additional. No package was
published and no tag was created.

**Choices made by the user** (asked before building)

- **Install:** one line naming four distributions,
  `pip install dataindex-core dataindex-ext-documents dataindex-ext-databases dataindex-llm`;
  no meta-package and no `[all]` extra.
- **Model backend:** a new `dataindex-llm` distribution with a standard-library
  OpenAI-compatible HTTP client by default and an optional
  `dataindex-llm[litellm]` extra.
- **Trust (`first_party_default`):** local mode trusts only the installed
  first-party free plugins, by name and installed version, and authorizes only the
  sources the user submits. It has no entitlement provider, so commercial
  capabilities stay denied. Premium and third-party plugins still need an
  explicit allowlist.
- **Extras (all five):** document management, `get_tree`/`get_section`, CLI
  `submit`/`chat`, `submit_database` with typed read-only queries, and agent-tool
  exports routed through the central dispatcher.

**Built**

- **Core** (`packages/core/src/dataindex/`): new `llm.py` (chat-model protocol and
  a strictly validated JSON planner), `workspace.py` (registry of submitted files;
  stores no file content), `local.py` and `local_runtime.py` (local defaults and
  the submit/inspect/chat workflow, built on the public client API and the
  central dispatcher), `outline.py` (bounded outlines from catalog nodes only),
  `database_config.py` (canonical typed query configuration), `agent_tools.py`,
  `cli_local.py` and `_json.py`. Reshaped `sdk.py`
  (`DataIndexClient(chat=..., storage_path=...)`), `cli.py`, `exceptions.py`,
  `reasoning.py`, `answers.py` and `catalog.py`. Core still depends only on
  `packaging>=23.2` and imports no extension or provider SDK.
- **Plugins:** documents gained `get_section` and `unregister_document`;
  databases gained `register_database_config`, `database_source_version` and
  `unregister_database`.
- **`packages/llm/` (`dataindex_llm`):** `OpenAICompatibleChatModel` (standard
  library HTTP; `https://` required except for loopback; redirects refused; API
  key redacted from errors; 2 MiB response limit; reads only `OPENAI_API_KEY` and
  `OPENAI_BASE_URL`), `LiteLLMChatModel` (lazy import) and `create_chat_model`.
  It requires `dataindex-core>=0.1,<0.2`.
- **Surface:** `DataIndexClient.submit_document`, `submit_database`,
  `list_documents`, `get_document`, `get_document_id`, `get_tree`, `get_section`,
  `delete_document`, `chat`, `agent_tools`, `as_openai_tools`,
  `as_anthropic_tools`, `agent_instructions` and `call_tool`; CLI `submit`,
  `submit-db --config`, `documents list|get|tree|section|delete` and `chat`
  (exit codes 0, 2 and 130).
- **Docs and CI:** README local-mode quick start, `docs/PYTHON_API.md`,
  `docs/REPOSITORY_AND_PACKAGING.md`, `docs/RELEASE_CHECKLIST.md`,
  `docs/SECURITY_AND_ENTITLEMENTS.md`, `docs/VERSION_0_1_SCOPE.md`, ADR-007,
  changelogs and READMEs of the four packages,
  `examples/contracts_database_config.json`, and the non-publishing
  `.github/workflows/release.yml` and `databases.yml`, which now build and test
  all five packages.

**Acceptance criteria.** These come from the user's request and the repository
rules; they are not rows of `docs/ACCEPTANCE_CRITERIA.md`. Each is reported on its
own.

| # | Criterion | Result | Evidence |
|---|---|---|---|
| 1 | Install with one `pip install` line; no meta-package or `[all]` extra | MET for local wheels only | The four documented wheels plus the synthetic premium wheel install offline into clean venvs on Python 3.11.15, 3.12.13 and 3.13.12 with `pip check` clean. `dataindex-core` with `dataindex-llm` also installs alone and pulls in only `packaging`. Nothing is on PyPI and the names are unverified. |
| 2 | Python usage: construct a client with a model, submit a file, ask, get a cited answer | MET against a loopback fake model | `readme_flow.py` runs the README snippets and prints `README FLOW OK; model requests: 2`. Citations carry source ID, version, native locator and operation. It asserts that the API key and local paths never reach the model and that resubmitting is `unchanged` without a warning. No real provider was called. |
| 3 | CLI: `submit`, `submit-db`, `documents ...`, `chat` | MET | `smoke_cli.py` exits 0 (submit, duplicate-name warning, `submit-db`, list/tree/get, `chat` with and without a model, `chat --json` with citations). Also covered by `tests/integration/test_local_cli_integration.py` and `packages/core/tests/test_cli_local.py`. |
| 4 | Document management: list, status, lookup by name, delete, stale detection | MET | `test_local.py`, `test_workspace.py`, `test_local_client_integration.py`; the README flow asserts list, status, name lookup and delete. |
| 5 | `get_tree` and `get_section` | MET | `test_outline.py`, `test_documents_sections.py` and the integration tests; the README flow and CLI smoke call both. |
| 6 | `submit_database` with typed read-only queries and no model-authored SQL | MET | `test_database_config.py`, `test_databases_config.py`, `test_contracts_database_config_example.py`. `examples/mixed_sources.py` (the golden question) prints 150000 and 90000 USD cents with cited Markdown and SQLite evidence on all three Pythons. |
| 7 | Agent-tool exports through the central dispatcher | MET | `test_agent_tools.py`; the README flow calls `as_openai_tools`, `as_anthropic_tools`, `agent_instructions` and `call_tool`. |
| 8 | `dataindex-llm` backend | PARTLY MET | OpenAI-compatible backend: 322 tests pass and 1 is skipped from the source tree (it needs the installed wheel, and passes in the wheel runs), using fakes and a loopback server. LiteLLM is tested only with stubs; the real library was never installed or exercised. No real provider endpoint was called. |
| 9 | Trust and security rules hold | MET in tests; one relaxation needs approval | `test_prompt_injection_local.py`, grants pinned to one submitted source, entitlement and source checks before every call, document-derived text sent only as untrusted data, and no secrets or local paths sent to the model. Trusting first-party plugins by default relaxes the earlier explicit-allowlist rule for local mode and needs maintainer approval. |
| 10 | Core-only isolation | MET | After uninstalling the plugins and the adapter: `pip check` is clean, `import dataindex` loads no `dataindex_ext_*` or `dataindex_llm` module, `extensions list` prints `No extensions discovered.`, and the unfiltered core suite passes (1755) on all three Pythons. CLI failures are clear and exit 2: documents plugin missing, no model, and a model name without `dataindex-llm`. |
| 11 | No embeddings, vector store, fixed-token chunking or `pageindex` import | MET | Core depends only on `packaging>=23.2`. A search of the package sources, tests and examples found no `pageindex`, provider-SDK or vector-library imports. No PageIndex code was copied, so `THIRD_PARTY_NOTICES.md` is unchanged. |
| 12 | Every answer has source ID, version, native locator and operation | MET | Evidence that was not retrieved raises `InvalidCitationError`; `test_answers.py`; the `chat --json` smoke; the README flow prints each citation. |
| 13 | Python 3.11 or newer | MET | Wheel runs on 3.11.15, 3.12.13 and 3.13.12 (below). |

**Wheel-based validation (release evidence).** Built the sdist and wheel of all
five packages with Python 3.13.12 (`python -m build --wheel --sdist` for
`packages/core`, `packages/extensions/documents`, `packages/extensions/databases`,
`packages/llm` and `tests/plugins/mock_premium`) from a clean copy of the
repository. The wheel metadata and entry points were inspected. Then, for each
Python, a clean venv installed the five wheels offline
(`--no-index --find-links` for the wheels and a local copy of pytest, packaging
and their dependencies), and the `release.yml` commands ran with no `PYTHONPATH`,
so imports resolved to the installed wheels:

```text
python -m pip check
python -m pytest packages/core/tests packages/extensions/documents/tests packages/extensions/databases/tests packages/llm/tests tests/plugins/mock_premium/tests tests/integration -q -p no:cacheprovider -k "not test_extensions_list_works_without_plugins"
python examples/mixed_sources.py
python -m pip uninstall -y dataindex-ext-documents dataindex-ext-databases dataindex-llm dataindex-ext-mock-premium
python -m pip check
python -m pytest packages/core/tests -q -p no:cacheprovider
python -m dataindex extensions list
```

| Python | `pip check` | Mixed suite | `mixed_sources.py` | After uninstall: `pip check`, core suite, `extensions list` |
|---|---|---|---|---|
| 3.11.15 | clean | 2675 passed, 1 deselected (216 s) | 150000 / 90000 USD cents, cited | clean; 1755 passed (71 s); `No extensions discovered.` |
| 3.12.13 | clean | 2675 passed, 1 deselected (222 s) | same | clean; 1755 passed (75 s); same |
| 3.13.12 | clean | 2675 passed, 1 deselected (166 s) | same | clean; 1755 passed; same |

The deselected test, `test_extensions_list_works_without_plugins`, asserts the
no-plugins state and runs in the core-only step. Also verified on 3.13.12:
`readme_flow.py` and `smoke_cli.py` against the installed wheels; the core wheel
carries no `License-File` (the unresolved license decision, below); and
`dataindex-llm` requires `dataindex-core>=0.1,<0.2` with a `litellm` extra.
A venv holding only `dataindex-core` and `dataindex-llm` passes `pip check`,
imports without loading plugin or provider modules, and a `litellm:` model name
fails at client creation with `pip install "dataindex-llm[litellm]"`.

**Source-tree runs** (each suite alone with `PYTHONPATH` set to the package
sources; the plugin suites use fake `dist-info` entries so entry points resolve):
core-only 1753 passed and 2 deselected (entry-point tests), databases 325 passed,
LLM 322 passed and 1 skipped, integration 160 passed and 2 skipped (mock premium
not installed), documents 106 passed.

**Source changes made after the test agents started.** Five background agents
wrote the test suites for this work. Their reports led to these edits. Each has a
regression test, and the parser and planner fixes were mutation-checked against
the earlier code. Timings are from Python 3.13.12 and include about 1 s of
interpreter startup.

| Area | Defect | Fix | Before, then after |
|---|---|---|---|
| Documents `readers.py` | The ATX heading regex backtracked on `# a` followed by blanks | linear `_atx_heading` | 4.5 s at 1,000 blanks and 44 s at 2,000; 5 MiB of spaces in 3.0 s |
| Documents `readers.py` | Markdown title parsing backtracked on unclosed `![` and `[x](` | title input capped at 512 characters (`MAX_TITLE_CHARS`) | 19 s at 20,000 and 86 s at 40,000; 2.6M `![` in 1.5 s |
| Documents `readers.py` | The HTML ignored-tag stack was quadratic (`<svg>` plus 100K nested tags) | O(1) handling of unmatched tags | timeout over 30 s; 300K levels in 8.4 s |
| Documents `readers.py` | HTML with 100K headings built every section before the limit check | early `DocumentTooLargeError` | over 40 s; 3.2 s |
| Documents `readers.py` | `\v`, `\f`, `\x1c`-`\x1e`, `\x85`, U+2028 and U+2029 counted as line breaks, bypassing the 100,000-line guard and disagreeing with editor line numbers | locators count only `\n`, `\r` and `\r\n` | 5 MiB accepted in 16-48 s; refused in 2.8 s (Markdown) and 3.5 s (text) |
| Core `llm.py` | The planner's code-fence regex was cubic on a fence followed by whitespace | `_unfenced` string helper | 4,000 spaces timed out; about 1 s |
| Core `catalog.py` | A corrupt or non-SQLite catalog file raised a raw `sqlite3` error and left the connection open | typed `CatalogError` without the path, connection closed | none |
| Core `answers.py` | A claim citing unretrieved evidence raised a raw `KeyError` | typed `InvalidCitationError` | none |
| Databases `sqlite.py` | `SELECT COUNT(*)` was denied because SQLite reports the table read with an empty column name | the empty column is allowed only for allowlisted tables in `main`; column reads still need allowed fields, and `sqlite_master`, `temp.*`, `pragma_*` and other tables stay denied | none |
| Core `local_runtime.py` | Stale-source messages named an SDK method, which misled CLI users | neutral "submit it again" wording | none |

Regression tests that run hostile inputs execute them in child processes with a
30 s timeout, because a regex match holds the GIL and an in-process timeout cannot
detect a hang. A differential fuzz of 600,000 random inputs found the new ATX and
fence code exactly equivalent to the old regexes for accepted input. Mutation
checks: the new documents tests fail against the pre-hardening `readers.py` (the
title cap, the three line-ending tests and the ATX and image-markup timeouts; the
slowest cases were deselected for time), and the three planner timeout tests fail
against the old fence regex while 12 behavioral cases pass under both versions.

These edits change Phase 03 and Phase 04 code after those phases were validated,
and only for unusual input: line numbering of text containing form feed, NEL or
U+2028/9; Markdown titles longer than 512 characters; HTML with too many headings
refused earlier; and `COUNT(*)` now allowed. Reviewers of the open Phase 03 and
04 gates should re-read `readers.py` and the authorizer in `sqlite.py`.

**Static checks.** No repository linter or type checker is configured. Pylance
syntax checks are clean for the changed modules and tests. LSP diagnostics are
clean for `llm.py` and `local_runtime.py`; the selected editor interpreter has
neither pytest nor `dataindex` installed, so `readers.py` and the two test files
report only `reportMissingImports`. A pattern search of source, tests, docs and
workflows found no secret-shaped strings; it does not replace the release secret
scan, which has not been run.

**Known limitations (accepted, not fixed)**

- *Behavior users can hit:*
  - A section larger than 256 KiB cannot be returned as evidence
    (`ToolEvidenceTooLargeError`).
  - A live SQLite database in WAL mode changes its version when written, so it
    must be submitted again.
  - A plugin release that changes indexing does not re-index an unchanged file;
    delete it and submit it again.
  - `documents list` and a failed `submit` create an empty `./.dataindex`
    directory; `chat` with a missing adapter does not.
  - Non-TTY `chat` reads a whole stdin line before the 2,048-character limit
    applies.
  - The documents 100,000-line limit counts a trailing newline as one more line,
    and CR-only HTML reports every heading at line 1 (`html.parser` counts only
    `\n`).
  - `html.parser` costs about 12-47 microseconds per tag, so 5 MiB of tiny tags
    can take 10-30 s. This is linear and was measured only on Python 3.13.12.
  - `chat --json` carries only a 400-character excerpt of database results.
  - `documents get` prints the local path (administrator output), and a reference
    of 257 or more characters gets a name-length message. `--storage` pointing at a
    file gives operating-system-specific text that includes the path.
  - The display-name charset is not tightened: `--name ../evil.md` is accepted
    (display only), and control, bidirectional and C1 characters pass validation.
    CLI output neutralizes them and JSON output is escaped.
  - Raw `sqlite3` errors after the catalog opens, such as "database is locked",
    are not in the CLI's handled-error list.
  - Denied column reads surface as `DatabaseQueryError`.
  - `query_definition_sha256` hashes fixed-filter values, so low-entropy fixed
    filters can be guessed from the digest, and fixed `source_filters` values are
    stored in plain text in `workspace.sqlite3`.
  - A column literally named `""` would match the authorizer's empty-column
    allowance.
- *Host and API edge cases:*
  - `agent_tool_definitions()` returns shared module-level `AgentTool` objects
    (the exports are copies).
  - `trusted_plugins=42` raises a raw `TypeError`, and an entitlement provider
    that returns garbage propagates `TypeError`.
  - NaN in a tool result gives a misleading "invalid arguments" message, and the
    `integer` schema type rejects `2.0`.
  - `close()` reports a plugin close failure only after releasing everything else.
  - `dispatch` words "not registered" and "not this tenant" differently, an
    existence oracle for direct host callers only. `ToolExecutionError.__cause__`
    keeps the plugin message (traceback only).
  - Oversized `keyword_hints`, locator values or warnings fail closed with a
    misleading `ReasoningBudgetExceededError`.
  - A plugin-controlled exception class name (80 characters at most) reaches the
    model in `failed_calls`.

**Release blockers** (the first four are unchanged from the Phase 07 record)

1. Human reviews of Phases 03-06, and of this addendum and ADR-007.
2. License decisions for the core, the free plugins and `dataindex-llm`. Each
   extension `LICENSE` is the "pending maintainer/legal review" placeholder and
   the core wheel has no `License-File`; wheel builds grant no redistribution
   rights.
3. PyPI name availability and ownership must be checked for every distribution,
   including the new `dataindex-llm`.
4. The CI workflows have never run: the workspace has no Git repository or remote.
   The dependency and license audit, secret scan and SBOM/provenance are also
   outstanding.
5. LiteLLM was never exercised with the real library, and the OpenAI-compatible
   backend was never run against a real provider.
6. Maintainer approval of the `first_party_default` trust relaxation.

**Decisions requested:** accept, change or reject ADR-007 and the first-party
default trust; decide whether LiteLLM ships in v0.1.0 given it is unexercised;
decide whether to tighten the display-name charset; and make the license and
package-name decisions above.

Local mode is complete for review and is not approved. Stop here for human
review; do not publish or tag `v0.1.0`.
