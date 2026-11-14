# Phase 05 — Bounded reasoning and multi-source grounded answers

**Release:** DataIndex **v0.1.0 FIRST IMPLEMENTATION**. **Phase status at pack creation:** NOT STARTED.

## Objective and dependencies
Use hierarchical traversal to select candidate sources. Optional model adapter can select from allowed tools; deterministic retrieval works with no LLM. Enforce structured planner output and evidence checks.

Prerequisite: Phase 04 acceptance gate passed with recorded tests.

## Exact tasks and deliverables
- Define `LLMPlanner` provider protocol without hardcoded vendor; optional reference adapter behind extras.
- Build deterministic source planner based on metadata and policy; optional LLM planning schema validated before dispatch.
- Implement bounded tool loop: depth/time/calls/tokens; record selection/execution summary (not private chain-of-thought).
- Central dispatcher validates permitted tool/source, plugin states, quota, tenant and future entitlements.
- Build answer composer with cited claims and abstention for missing evidence; forbid generated citations absent in records.
- Add prompt-injection fixtures and multi-source demo: overdue invoice totals + contract terms.

## Acceptance criteria — must prove with actual tests/evidence
- Deterministic no-model query path works.
- Mock LLM selects real tools for mixed SQL/document question; matches known answer/evidence fixtures.
- Tool execution cap/authorization failures are enforced.
- Hallucinated citation and instruction injection are rejected or flagged.

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
