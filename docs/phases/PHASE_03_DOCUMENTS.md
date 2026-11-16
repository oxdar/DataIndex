# Phase 03 — First free extension: DocumentIndex

**Release:** DataIndex **v0.1.0 FIRST IMPLEMENTATION**. **Phase status at pack creation:** NOT STARTED.

## Objective and dependencies
Create a fully separate FREE wheel, not a core optional-extra masquerading as an extension. Preserve text natural sections and exact source locators. No fixed-token chunk indexing.

Prerequisite: Phase 02 acceptance gate passed with recorded tests.

## Exact tasks and deliverables
- Create `packages/extensions/documents/pyproject.toml` with independent wheel and entry point `documents`.
- Implement Markdown, TXT and HTML readers with clear format-specific boundaries and locators. The approved user-requested scope addition also covers tested PDF, Office, OpenDocument, RTF, EPUB, EML, CSV/TSV, JSON/JSONL and XML readers; parsers that need third-party packages remain optional extras.
- Expose `probe`, `index`, `retrieve` and `health` via public contract; include size limits and non-executable HTML handling.
- Create citation-backed section retrieval and derived summaries marked as generated if used.
- Test independent wheel installs, plugin discover/enable/disable, unsupported format errors and security behavior.
- Write extension README, license, NOTICE and examples.

## Acceptance criteria — must prove with actual tests/evidence
- `dataindex-core` wheel alone remains usable.
- Install document wheel separately: correctly discovers and indexes every advertised format with tested natural sections and native locators; the core-only install remains usable without optional parsers.
- Retrieved answer includes actual source version and section locator.
- Uninstall document wheel: core and other extensions still function.

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
