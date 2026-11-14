---
description: "DataIndex v0.1.0 | Phase 01: First production code: independently installable core"
---
# DataIndex v0.1.0 — Phase 01

You are acting as the Principal Engineer for the FIRST DataIndex release. This is a GREENFIELD project, not an upgrade or migration from v0.2.

Read `AGENTS.md`, `.github/copilot-instructions.md`, `docs/VERSION_0_1_SCOPE.md`, `docs/SYSTEM_ARCHITECTURE.md`, `docs/EXTENSION_CONTRACT.md`, `docs/REPOSITORY_AND_PACKAGING.md`, `docs/SECURITY_AND_ENTITLEMENTS.md`, `docs/ACCEPTANCE_CRITERIA.md`, and `docs/phases/PHASE_01_CORE_PACKAGING.md` completely before editing.

Implement **Phase 01: First production code: independently installable core** ONLY, following every deliverable and acceptance test in the detailed phase file. Modify code and tests in the workspace, not just descriptions. Preserve the work completed in prior phases.

Mandatory constraints: no runtime `pageindex` dependency, no embeddings/vector DB/fixed-token indexing, independently buildable free or premium-extension Python distributions, source-authorized native retrieval, source-backed evidence. If adapting PageIndex source, fetch and audit actual upstream MIT code with notices.

After implementation, run appropriate tests and wheel-install matrix, show exactly what passed and failed, update `docs/PHASE_STATUS.md`, then STOP and await review. Do not declare later phases completed.
