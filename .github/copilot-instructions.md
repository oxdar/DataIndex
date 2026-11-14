# DataIndex — GitHub Copilot repository instructions

## Baseline
**Empty / greenfield. This project has NEVER shipped DataIndex v0.1.0.** This workspace initially holds only specifications, agents' instructions and Copilot prompts. DO NOT search for a fictional DataIndex v0.2 baseline, migrate a prior starter or presume that any Python code exists.

## Mission
Create DataIndex **v0.1.0 from scratch**: a minimal open-source, vectorless reasoning retrieval core with individually installable free and premium plugins. Build and verify each phase in `docs/IMPLEMENTATION_PLAN.md`. The v0.1.0 scope is explicitly defined in `docs/VERSION_0_1_SCOPE.md`.

## Architectural rules
- Core import namespace `dataindex`; intended distribution `dataindex-core`; verify PyPI availability before publishing.
- Never import `pageindex` at runtime. Inspect and selectively copy/adapt PageIndex MIT source into DataIndex when useful; preserve original license notices and record exact provenance.
- No vector DB, embedding generation/search, or arbitrary fixed-length token chunks. Native document boundaries, scene hierarchy, spatial trees and tiles are allowed.
- Core must install and import on Python 3.11+ without PDF/OCR/DB-driver/scientific/GPU libraries, an LLM credential, or licensing network.
- Free and paid extensions use the *same* typed contract. Each is independently packaged, versioned, installed, discovered, enabled and upgraded.
- Use Python `dataindex.extensions` entry-point group. Missing/incompatible/untrusted/unlicensed plugins must fail gracefully without disabling other plugins.
- Keep premium implementations out of the public repository. A mock premium extension is permitted only for contract tests and demonstrations.
- License entitlement and source access authorization are separate checks. Perform both in the central tool dispatcher and enforce server authorization in hosted deployment. License status alone never grants access to confidential data.
- Plugin execution is arbitrary Python execution. Run only explicitly trusted packages in-process. Offer isolation in future enterprise design; never call the entry-point registry a sandbox.
- Index typed metadata, IDs, relationships, summaries and precise locators. Native source remains authoritative. No bulk DB row dumps / seismic cube dumps into prompts.
- Use deterministic source operations (SQL, metrics, geometry) for calculations; LLM coordinates retrieval but does not invent facts.
- Build small real vertical slices, not sprawling folders of TODO implementations.

## Work discipline
1. Read phase plan, dependency docs and tests.
2. State the files and behaviors you will create for this phase.
3. Implement real code, fixtures, unit/integration tests, and docs.
4. Run build/test/lint/static checks. If unavailable, say so.
5. Record status in `docs/PHASE_STATUS.md` and report acceptance criteria individually.
6. STOP after the phase; request review before advancing.

Follow `AGENTS.md`; `docs/ACCEPTANCE_CRITERIA.md` defines release blockers.
