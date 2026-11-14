# ADR-004: Trusted plugin execution and central policy enforcement

- **Status:** Proposed for Phase 00 review
- **Date:** 2026-11-13

## Context

Python entry points load executable code. Commercial entitlement is not source
authorization, and listing a plugin cannot safely imply that its tools may run.

## Decision

Keep installed, discovered, compatible, trusted, enabled, entitled,
tenant-authorized and healthy as separate states. Read static entry-point
metadata/manifests without importing plugin code. Require an administrator
trust rule and API compatibility before loading a plugin; verify its runtime
manifest against static metadata. Unknown plugins remain unloaded. Treat
in-process plugins as trusted arbitrary code, never as sandboxed.

The central dispatcher is the only retrieval execution boundary. For each
request, authenticate identity, authorize source and operation, resolve a trusted
compatible enabled/healthy tool, check capability entitlement when premium,
apply resource limits, execute and validate provenance. Enforce both source
authorization and entitlement server-side in hosted deployments.

## Consequences

- Entitlement is checked for the exact capability at execution and never grants
  access to files, rows or tenants.
- Plugin/import/health failures are explicit and isolated to that plugin.
- Core/free extensions need no licensing network.
- Process isolation is future work; an admin allowlist is a trust decision, not
  a code sandbox.

## Alternatives rejected

- Trust-on-discovery executes unknown code.
- A single `active` flag hides incompatible, unauthorized or untrusted states.
- Client-only licensing cannot secure proprietary implementation source.
