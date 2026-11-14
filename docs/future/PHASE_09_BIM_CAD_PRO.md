# Future Phase 09 — BIMIndex & CADIndex private commercial extensions

**Not part of v0.1.0.** Implement in authorized PRIVATE repositories, each independently published and entitled.

### BIMIndex Pro — first pilot candidate
- Primary IFC via IfcOpenShell. Index project/building/storey, elements, IFC GUIDs, property sets, containment and system relationships; validate file-version IDs.
- Native element locator and operations (`element_properties`, `elements_by_storey`, `connected_systems`) with source access policy.
- Do not promise direct proprietary RVT parsing without tested authorized SDK/conversion.

### CADIndex Pro
- Start with STEP and DXF; assemblies, layers, blocks, geometry/units and deterministic measurements.
- Evaluate Open Cascade and ezdxf; precise NURBS/solids differ from mesh approximation.
- DWG and IGES evaluated separately after license and format coverage review.

### Commercial tests
- Each wheel installs from separate approved private artifact, absent entitlement blocks paid retrieval at central dispatcher; source auth still required.
- A disconnected on-prem signed entitlement can be verified offline with public key (if that license mode is offered).
- Revocation/expiration and tenant isolation tested.
- Private implementation source never checked into public repository.

### Acceptance
- IFC fixture: correct GUID, storey and property retrieval; no fabricated connected system.
- STEP/DXF fixture: correct native part/block and dimension with units.
- Free plugins remain operational when commercial license is absent.

## Copilot protocol
Read `AGENTS.md`, extension contract, security and packaging docs. Implement this phase only when explicitly authorized after `v0.1.0` release. Build an independent wheel with actual format fixtures, unit/integration/security tests, native evidence locators, and no core regression. Do not mark supported formats until tests pass.
