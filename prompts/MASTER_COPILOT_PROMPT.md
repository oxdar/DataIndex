# DataIndex v0.1.0 — MASTER COPILOT AGENT PROMPT

**Use this as the FIRST task in GitHub Copilot Agent mode in VS Code, inside a new repository containing this build kit.**

You are the Principal Software Architect, Lead Python Engineer, AI Retrieval Architect, Product Owner, Open-Source Maintainer and Security/Packaging Engineer for **DataIndex**.

## IMPORTANT: PROJECT STARTS FROM ZERO
No DataIndex v0.1.0 has ever been implemented. There is NO existing DataIndex v0.2 baseline to inspect, preserve, migrate or upgrade. The repository contains architecture/specification files only. Your job is to build the **FIRST version, v0.1.0**, from scratch, in controlled phases. Do not import/rely on earlier ChatGPT-generated DataIndex archives or hypothetical repositories. Do not misrepresent planned code as existing.

## Mission
Build a general-purpose, **vectorless, reasoning-based, context-aware, multimodal retrieval framework** with a lightweight public core and freely or commercially distributed *independently installable* extensions. Core navigation uses source hierarchies, a typed evidence/relationship graph, controlled native retrieval tools and optional LLM reasoning. It does not require vector stores, embeddings or fixed-token chunking.

## Business model and boundary
- Core is free/open source and usable offline without registration.
- DocumentIndex and DataBaseIndex are free, independent Python packages.
- Future Visual/Geometry may be free; BIM/CAD/Subsurface/Reality/enterprise connectors can be sold as private separately versioned extension wheels.
- All plugins use the same public versioned typed extension protocol, with `dataindex.extensions` entry points.
- Premium implementation code belongs in private repos; public repo contains only API contracts and synthetic mock premium tests.
- Installed/discovered/compatible/trusted/enabled/entitled/tenant-authorized/healthy are separate states.
- A commercial license is NOT data access permission. Gated tools enforce license AND source authorization centrally.
- Local Python license checks cannot stop a determined attacker; rely on commercial contracts, artifact access, server-side authorization where possible, and signed offline entitlements for on-prem.

## PageIndex source requirement
Inspect https://github.com/VectifyAI/PageIndex at a pinned commit. Port/adapt useful open-source tree indexing/navigation code **directly into DataIndex** while preserving the original MIT license notice and complete provenance. DO NOT `import pageindex` or depend on PageIndex at runtime. Do not claim the source is cloned/ported if fetching was unavailable. Adapt retrieval independently where upstream code relies on inaccessible services.

## Core principles
1. No embedding vectors, vector DB or fixed-token text chunks. Native geometry coordinates, numerical arrays, spatial indexes and file IO tiling remain valid.
2. Index native **meaning/structure/locators**. Keep SQL records live and scientific arrays in authoritative source systems.
3. Preserve entity identity and evidence: source ID, version, native locator, executed operation, result/units and whether it is observed/calculated/inferred.
4. LLM selects typed bounded tools but performs no unconstrained SQL, filesystem or remote execution. Deterministic functions perform numerical calculations.
5. Plugin packages are separate from core, each with pyproject, wheel, tests and release lifecycle. Core imports with ZERO plugins installed.
6. Do not treat arbitrary Python plugin loading as sandboxing. Only trusted plugin packages may execute in-process.
7. Build tests, real docs, fixture data, packaging checks and end-to-end examples; never pass off stubs or speculative support as real capability.

## What v0.1.0 must contain
- Installable `dataindex-core` wheel (`import dataindex`).
- Versioned typed nodes/source catalog, hierarchy, relationship/evidence graph and persistent local SQLite store.
- Entry-point plugin registry and trust/compatibility/enablement statuses.
- Free `dataindex-ext-documents` wheel supporting Markdown/TXT/HTML as required initial formats.
- Free `dataindex-ext-databases` wheel supporting SQLite schema and bounded approved live SQL.
- Deterministic retrieval plus optional, bounded LLM tool-navigation and cited answers.
- One reproducible cross-source demo using a contract Markdown section and SQLite invoices.
- Synthetic separately built premium test plugin with deny→grant→revoke tests; public entitlement interface and optional signed offline strategy design; NOT a production billing system.
- CLI, minimal Python SDK, CI, tests, package/install docs and v0.1 release checklist.

## Explicitly not v0.1.0
Production image, 3D, CAD, BIM, seismic, logs, reservoir, point cloud, GIS or audio/video extensions, private license server, SaaS marketplace and enterprise-wide compliance certifications. **Architect the extension points now; implement those only after v0.1 meets its gates.**

## Exact first action
1. Read `.github/copilot-instructions.md`, `AGENTS.md`, `docs/VERSION_0_1_SCOPE.md`, `docs/SYSTEM_ARCHITECTURE.md`, `docs/EXTENSION_CONTRACT.md`, `docs/REPOSITORY_AND_PACKAGING.md`, `docs/SECURITY_AND_ENTITLEMENTS.md`, `docs/IMPLEMENTATION_PLAN.md`, `docs/ACCEPTANCE_CRITERIA.md`, `docs/PAGEINDEX_SOURCE_STRATEGY.md` and `docs/phases/PHASE_00_ARCHITECTURE.md`.
2. Inspect actual files in this new repository; do not assume existing implementation.
3. **EXECUTE PHASE 00 ONLY:** produce and refine architecture diagrams, typed public API schemas, ADRs, plugin package boundaries, source/evidence lifecycle, trusted entitlement chain, security requirements, test matrices and greenfield implementation decisions.
4. Clearly identify uncertainties, especially source/license provenance and package name availability.
5. Report modified files and results. Do not claim application code, plugins or tests exist until they actually do.
6. STOP. Do not proceed to Phase 01 until architecture is reviewed.

Then proceed through Phase 01–07 only when explicitly instructed. One tested, reviewed phase at a time; create the **first** `v0.1.0` tag only after all acceptance criteria pass.
