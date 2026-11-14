# Phase 04 — Second free extension: DataBaseIndex

**Release:** DataIndex **v0.1.0 FIRST IMPLEMENTATION**. **Phase status at pack creation:** NOT STARTED.

## Objective and dependencies
Add database semantics natively rather than exporting whole tables as Markdown. SQLite is the initial required end-to-end adapter; PostgreSQL may follow.

Prerequisite: Phase 03 acceptance gate passed with recorded tests.

## Exact tasks and deliverables
- Create `packages/extensions/databases/pyproject.toml` and entry point `databases`.
- Discover SQLite tables, fields, PK/FK, safe metadata; index schema and relationship graph only.
- Implement table/source allowlists and approved parameterized SELECT templates with filters, join policies, aggregates, row caps and execution budget.
- Use read-only database credentials/connection mode; test rejected DML/DDL, malicious SQL text and unauthorized columns.
- Return source key, read timestamp, query ID, units if applicable and result provenance.
- Add demo SQLite `contracts`/`invoices` and sample queries; no huge materialization.

## Acceptance criteria — must prove with actual tests/evidence
- Correct schema/PK/FK discovery.
- Live query returns real current row values and deterministic SUM/COUNT results.
- No `UPDATE`/`DROP` or model-generated arbitrary SQL reaches SQLite.
- Database plugin install/uninstall is independent of DocumentIndex.

## Required validation evidence
- List concrete files changed and architecture decisions.
- Execute unit/integration checks applicable to this phase, show exact shell commands and actual outputs/results.
- Where a wheel is introduced or modified, build and install it in a clean isolated environment; run `pip check`.
- Inspect whether optional plugin imports break core-only installation.
- Document unsupported behavior and security/operational risks.
- Update `docs/PHASE_STATUS.md`; stop at phase boundary for human review.

## Do not do
- Do not move to later phases in this change set.
- Do not claim future extensions or commercial packages already exist.
- Do not claim a successful integration without real sample files and tests.
