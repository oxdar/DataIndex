# Phase 07 — Complete first release v0.1.0

**Release:** DataIndex **v0.1.0 FIRST IMPLEMENTATION**. **Phase status at pack creation:** NOT STARTED.

## Objective and dependencies
Turn all previous work into a reproducible first-release candidate. No claim of BIM/CAD/Subsurface production support. Verify actual package names/availability before external publication.

Prerequisite: Phase 06 acceptance gate passed with recorded tests.

## Exact tasks and deliverables
- Finalize documented Python SDK: initialize catalog; register sources; index; retrieve; ask; list extensions.
- Ship CLI for index/list/query/extension inspect, with secure-by-default config.
- Optional minimal REST API only if this does not risk release quality; document omission otherwise.
- Build source/sdist and separate wheels in CI; run clean-install matrix on supported OS/Python combinations.
- Add unit/integration/security golden tests, package/notice/license audit, dependency scan, provenance and SBOM if supported.
- Write tested README quickstart, CHANGELOG, CONTRIBUTING, security disclosure, full API reference and v0.1.0 release notes.
- Update `docs/PHASE_STATUS.md` with actual green tests and create signed/reproducible tag only after signoff.

## Acceptance criteria — must prove with actual tests/evidence
- Core-only and each independent plugin wheel installs and passes `pip check`.
- Full mixed source retrieval and premium denial/grant tests pass.
- No runtime PageIndex dependency; no embedding/vector DB dependency.
- Docs correctly distinguish 0.1 built features from all future paid modules.

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
