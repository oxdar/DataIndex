---
description: "DataIndex v0.1.0 | Phase 00: Initial greenfield blueprint"
---
# DataIndex v0.1.0 — Phase 00

You are acting as the Principal Engineer for the FIRST DataIndex release. This is a GREENFIELD project, not an upgrade or migration from v0.2.

Read `AGENTS.md`, `.github/copilot-instructions.md`, `docs/VERSION_0_1_SCOPE.md`, `docs/SYSTEM_ARCHITECTURE.md`, `docs/EXTENSION_CONTRACT.md`, `docs/REPOSITORY_AND_PACKAGING.md`, `docs/SECURITY_AND_ENTITLEMENTS.md`, `docs/ACCEPTANCE_CRITERIA.md`, and `docs/phases/PHASE_00_ARCHITECTURE.md` completely before editing.

Implement **Phase 00: Initial greenfield blueprint** ONLY, following every deliverable and acceptance test in the detailed phase file. Do architecture and ADR documentation first, without pretending code exists.

Mandatory constraints: no runtime `pageindex` dependency, no embeddings/vector DB/fixed-token indexing, independently buildable free or premium-extension Python distributions, source-authorized native retrieval, source-backed evidence. If adapting PageIndex source, fetch and audit actual upstream MIT code with notices.

After implementation, run appropriate tests and wheel-install matrix, show exactly what passed and failed, update `docs/PHASE_STATUS.md`, then STOP and await review. Do not declare later phases completed.
