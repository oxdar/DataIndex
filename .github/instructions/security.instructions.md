---
applyTo: "src/dataindex/security/**,packages/**/security/**,src/dataindex/retrieval/**"
---
# Security rules
- License entitlement never substitutes for user/tenant/source authorization.
- Check tenant, source, operation, entitlement, quotas and resource limits at centralized execution boundaries.
- Do not claim Python entry-point plugins are sandboxed; require trusted package provenance and administrator approval.
- No arbitrary SQL, shell, network URL fetch or dynamic Python from model-generated text.
- Keep secrets out of catalogs, prompts, logs, sample files and tests.
- Treat documents, plugins and model outputs as untrusted; prompt-injection tests are mandatory.
