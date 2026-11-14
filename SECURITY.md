# Security policy

The v0.1.0 candidate is not a hosted service and has no supported release or
security-response SLA yet. Do not send sensitive source data, credentials, or
private extension code in a public issue.

For a suspected vulnerability, use GitHub's private vulnerability reporting
feature for this repository if it is enabled. Otherwise contact repository
maintainers privately through GitHub. Do not publish exploit details before
coordinating a fix.

Important boundaries:

- Installed extensions execute arbitrary Python in-process. Trust only
  explicitly reviewed distribution/version pairs; the registry is not a
  sandbox.
- Local CLI tenant labels are not authentication. Protect catalog files and
  use separate files across trust boundaries.
- Hosted deployments must independently enforce identity, source authorization
  and entitlement at the server dispatcher. License status alone never grants
  source access.
- The database extension supports only predeclared, bounded, read-only query
  definitions. Never pass agent-authored SQL.
- Source locators and evidence values can contain sensitive data. Apply
  application-level access controls, retention and logging policies.

Report the affected package/version, deployment context, impact and a minimal
reproduction privately. Do not include production data or secrets.
