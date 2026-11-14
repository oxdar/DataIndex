# Phase 06 — License-ready architecture, without shipping paid modules

**Release:** DataIndex **v0.1.0 FIRST IMPLEMENTATION**. **Phase status at pack creation:** NOT STARTED.

## Objective and dependencies
Prove independently installed paid extensions can be controlled without holding back free packages. Use only synthetic test premium plugin and mock entitlements. Do not build a production billing portal.

Prerequisite: Phase 05 acceptance gate passed with recorded tests.

## Exact tasks and deliverables
- Create `EntitlementProvider` typed protocol with `check(tenant, capability, deployment)` result and auditable denial reasons.
- Add in-memory test provider and optionally a signature-verification test fixture; no hardcoded secret.
- Build `tests/plugins/mock_premium` as independent wheel that uses **the same** entry-point and public protocol as free plugins.
- Implement central gated tool dispatch: installed/discovered/compatible/trusted/enabled/entitled/authorized/healthy state.
- Demonstrate deny→grant→expiry/revoke and cross-tenant denial, with no impact on free DocumentIndex/SQL.
- Document future hosted/offline licensing architecture, private package distribution and limits of local tamper protection.

## Acceptance criteria — must prove with actual tests/evidence
- Independent premium wheel is visible as installed but tool call fails without entitlement.
- With authorized mock entitlement and resource permission, tool executes and emits provenance.
- Revocation blocks later calls without stopping free plugins.
- No premium source or provider billing secrets in public repo.

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
