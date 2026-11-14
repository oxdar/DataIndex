# Phase 01 — First production code: independently installable core

**Release:** DataIndex **v0.1.0 FIRST IMPLEMENTATION**. **Phase status at pack creation:** NOT STARTED.

## Objective and dependencies
Build `packages/core` from zero. The core imports with only standard/minimal dependencies, without any optional plugin or LLM provider. Implement the extension discovery contract and basic CLI listing.

Prerequisite: Phase 00 acceptance gate passed with recorded tests.

## Exact tasks and deliverables
- Create `packages/core/pyproject.toml` with `dataindex-core`, src layout, Python >=3.11, and `dataindex` namespace.
- Create typed `SourceRef`, `Node`, `NativeLocator`, `EvidenceRecord`, `Relationship`, `ExtensionManifest`, error types.
- Implement extension entry-point discovery using `dataindex.extensions` with unique IDs, version compatibility, declared capabilities and trusted allowlist.
- Keep plugin module loading separate from metadata discovery where feasible; do not claim true sandboxing.
- Add `dataindex extensions list` / minimal CLI and stub-free API.
- Add CI and tests for core-only install, no optional imports, duplicate IDs and incompatibilities.
- Review actual PageIndex source and port strictly justified tree routines into the core, preserving MIT notices and port-specific tests. If upstream cannot be fetched, report blocker rather than fabricating a port.

## Acceptance criteria — must prove with actual tests/evidence
- Clean venv installs core wheel and imports `dataindex`.
- Core works offline with zero plugins or LLM credentials.
- Entry-point registry behaves correctly with zero, one, duplicate and incompatible entries.
- No runtime `pageindex` dependency; adapted source provenance recorded if copied.

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
