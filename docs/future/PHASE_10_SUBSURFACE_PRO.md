# Future Phase 10 — SubsurfaceIndex private commercial extension

**Not part of v0.1.0.** Separate independently licensed `dataindex-ext-subsurface-pro` distribution and private repo.

### Well log adapter — first supported domain
- LAS with `lasio`, DLIS with `dlisio` only after fixture tests. Index curve types, mnemonics, units, depth interval, MD/TVD reference, nulls and well ID.
- Retrieval tools: read bounded depth interval, deterministic mean/median/ranges, curve plot data; invalid unit/depth alignment must raise a typed error.

### Reservoir adapter
- GRDECL/EGRID/INIT/UNRST and RESQML selectively after library feasibility review (`xtgeo`, `resqpy`).
- Index model version, grid-cell references, property name, units, simulation timestep, CRS/datum.
- Retrieve bounded cells/regions; no copying entire grid arrays into a text index.

### Seismic adapter
- SEG-Y headers, survey extents, coordinates, sample intervals, inlines/crosslines; `segyio` for bounded trace retrieval.
- Evaluate OpenVDS where scales require random-access large volumes. No claim of automatic geologic interpretation from metadata.

### Cross-domain reasoning
- Entity matching of well/reservoir/survey requires verified IDs, spatial transforms and alignment. Numerical operations must be deterministic, cite curves/cells/traces and show units.

### Acceptance
- Independent private wheel, entitlement + tenant authorization gates.
- Fixture-based LAS curve retrieval and unit-correct statistics; reservoir/seismic as separate acceptance milestones, not implied by well-log success.
- Never load full volumes into LLM context; evidence locators and source versions verified.

## Copilot protocol
Read `AGENTS.md`, extension contract, security and packaging docs. Implement this phase only when explicitly authorized after `v0.1.0` release. Build an independent wheel with actual format fixtures, unit/integration/security tests, native evidence locators, and no core regression. Do not mark supported formats until tests pass.
