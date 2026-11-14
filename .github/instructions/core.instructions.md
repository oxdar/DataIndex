---
applyTo: "src/dataindex/**,packages/core/**"
---
# Core rules
- No imports from any `dataindex_ext_*` distribution.
- Keep core dependencies minimal; no scientific, CAD, BIM, OCR, database driver or license-server dependency for `import dataindex`.
- All source access goes through typed, authorized tools and evidence results.
- Index and graph formats must be versioned and schema-migratable.
- Avoid provider-specific APIs in data models; optional adapters implement LLM interface.
