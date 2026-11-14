---
description: "Design a private paid DataIndex extension without leaking proprietary code"
---
Read `docs/EXTENSION_CONTRACT.md`, `docs/REPOSITORY_AND_PACKAGING.md`, `docs/SECURITY_AND_ENTITLEMENTS.md`, and `docs/EXTENSION_CATALOG.md`. Implement commercial extension **ONLY in a separate authorized PRIVATE repository**; never add proprietary source to the public DataIndex repo. Use identical public API to free plugins, independently published wheel, package trust, tenant/resource authorization, operation-specific entitlement, evidence provenance and test matrix. Verify actual license policy. For public repository work, create only contract fixtures/mocks and public docs. Do not pretend licensing is tamper-proof.
