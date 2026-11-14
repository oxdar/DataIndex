# Phase 00 — Initial greenfield blueprint

**Release:** DataIndex **v0.1.0 FIRST IMPLEMENTATION**. **Phase status at pack creation:** NOT STARTED.

## Objective and dependencies
Do **not** audit a non-existent DataIndex v0.2. Inspect only this build pack and upstream PageIndex source availability. Decide the initial source, plugin, evidence, graph and entitlement models before any production code. Resolve which upstream functions are actually reusable.

Prerequisite: None; this is the initial design phase.

## Exact tasks and deliverables
- Create `docs/adr/ADR-001-open-core-plugins.md`, ADR-002 for evidence/index persistence, ADR-003 for PageIndex port provenance, ADR-004 for policy boundaries, ADR-005 for LLM planner and non-embedding design.
- Finalize `docs/SYSTEM_ARCHITECTURE.md` with Mermaid diagrams for indexing, retrieval, installation, and commercial activation.
- Finalize public Python model definitions and compatibility rules in `docs/EXTENSION_CONTRACT.md`.
- Confirm v0.1 minimal formats and separate installation matrices.
- Verify MIT requirements for adapted PageIndex code and review dependency licensing.
- Record unresolved licensing/packaging names explicitly as decisions before publishing.

## Acceptance criteria — must prove with actual tests/evidence
- Architectural responsibilities are unambiguous; v0.1 in-scope/out-of-scope documented.
- No assumed working DataIndex code; no historical migration.
- Separate free wheel vs private commercial wheel boundary drawn.
- Proposed test matrix covers core-only, core+one free plugin, mixed sources and premium-denial case.

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
