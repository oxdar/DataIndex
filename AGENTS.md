# AGENTS.md — DataIndex v0.1.0 (FIRST IMPLEMENTATION)

You are building a NEW repository from ZERO. No DataIndex v0.1, v0.2 or legacy implementation exists or needs migration.

1. Read `.github/copilot-instructions.md`, `docs/VERSION_0_1_SCOPE.md`, `docs/IMPLEMENTATION_PLAN.md`, and the current phase plan before edits.
2. Phase 00 is architecture-first. Subsequent phases create tested code. Do not run ahead or pretend a planned extension is implemented.
3. Core distribution `dataindex-core` exports Python package `dataindex`. The distribution name is proposed: verify package namespace availability before public publication.
4. Every extension is an **independent wheel**, versioned and installed separately. Plugin discovery is through `importlib.metadata.entry_points(group="dataindex.extensions")`. No unconditional extension imports in core.
5. Keep future premium production implementations in private repos. The public repo may contain only interface schemas and **synthetic premium demo/test fixtures**, never actual proprietary source.
6. The default pipeline has NO embeddings, vector DBs, fixed-token indexing or pageindex runtime dependency. Native spatial/numerical vectors, storage tiles and section segmentation are allowed.
7. Index source *structure* and native locators, not entire SQL tables, scientific volumes, CAD meshes or user data into LLM context.
8. Do not treat installed plugin code as sandboxed. Use explicit trusted plugin allowlists; central authorization AND entitlement checks before tool execution. Free modules must operate without a licensing service.
9. SQL: read-only identity, explicitly allowed tables/columns/queries, bound parameters, bounded output, timeouts; never execute agent-authored arbitrary SQL.
10. Every answer needs source ID, version, native locator and retrieval operation. LLM summaries and inferred entity links are not factual proof.
11. Preserve Vectify AI's MIT notices when reusing PageIndex source. Track exact commit, source files and transformations in `THIRD_PARTY_NOTICES.md`.
12. Tests first where possible, run checks and report actual results. A feature is not supported until representative fixtures and end-to-end integration tests pass.
13. One phase per branch/change set. Stop at acceptance gate and present results to the user for review.
