# Phase 02 — Persistent hierarchy and evidence graph

**Release:** DataIndex **v0.1.0 FIRST IMPLEMENTATION**. **Phase status at pack creation:** NOT STARTED.

## Objective and dependencies
Implement the first actual retrieval-independent typed index in core. SQLite stores metadata, stable IDs, parent-child tree structure and typed graph edges. Preserve native source locators and revisions.

Prerequisite: Phase 01 acceptance gate passed with recorded tests.

## Exact tasks and deliverables
- Build SQLite catalog with schema version, migrations and transaction handling.
- Implement create/update/remove source and node operations; hash/version metadata; stable IDs and parent-child constraints.
- Implement graph edges (explicit type, provenance and identity confidence) and traversal APIs.
- Implement natural-structure navigation, bounded deterministic keyword filters (SQLite FTS optional) and locator dereferencing abstraction.
- Implement evidence schema and verifier that rejects fabricated/missing source IDs/locators.
- Include tiny in-memory source fixture for core-only tests, no document parser dependency.

## Acceptance criteria — must prove with actual tests/evidence
- Create index, close process, reopen, navigate and cite same source/version.
- Duplicate node IDs, inconsistent parent relations, missing source revision and unauthorized cross-tenant links rejected.
- Graph relationships cannot silently become verified without source evidence.
- Tests run without LLM credentials or plugins.

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
