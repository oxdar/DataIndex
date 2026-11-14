# Future Phase 12 — Commercial licensing / deployment services after v0.1

**Not part of v0.1.0.** v0.1 ships protocol + synthetic premium fixture, not a billing product.

### Hosted entitlement service
- Signed claims or token introspection: tenant, deployment, plugin, feature/operation, quota, start/end, renewal/grace. Secure key rotation, audit decisions, rate limits and revocation.
- Billing gateway integration only after confirming merchant eligibility, provider contractual rules, and customer willingness to pay.
- No raw client documents needed for licensing.

### Enterprise on-prem licensing
- Signed offline entitlements with approved clock/expiry behavior, deployment binding and revocation limitations described honestly.
- Never claim Python plugin licensing is tamper-proof; protect private distribution and maintain commercial contracts.

### Marketplace/catalog
- Display installable package metadata, license tiers, verified publisher, compatible API/core version, capabilities and private channel instructions.
- Marketplace must not execute or auto-load unknown third-party packages silently.

### Acceptance
- Hosted tests: tenant A never consumes B entitlement. Admin revocation applied to subsequent tool calls.
- On-prem test: signed valid/malformed/expired entitlements; no secret key distributed.
- Core/free plugins run with entitlement service unavailable.

## Copilot protocol
Read `AGENTS.md`, extension contract, security and packaging docs. Implement this phase only when explicitly authorized after `v0.1.0` release. Build an independent wheel with actual format fixtures, unit/integration/security tests, native evidence locators, and no core regression. Do not mark supported formats until tests pass.
