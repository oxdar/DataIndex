# ADR-002: Typed hierarchy, evidence and local persistence

- **Status:** Proposed for Phase 00 review
- **Date:** 2026-11-13

## Context

Retrieval must navigate source structure without replacing native sources or
placing bulk records/assets into model context. Results must remain auditable
against exact source revisions and locators.

## Decision

Define versioned typed `SourceRef`, `NativeLocator`, `IndexRecord`,
`Relationship` and `EvidenceResult` models. Use opaque IDs, explicit source
versions/read markers, extension-defined versioned native locators and
JSON-compatible metadata. Keep catalog hierarchy and typed evidence
relationships distinct. Persist the local catalog in SQLite with an explicit
schema version and migration path.

Index source structure, IDs, typed metadata, relationships and locators.
Summaries may be marked machine-generated; inferred relationships are not
factual proof. The authoritative document, database or native asset remains
outside the index. On source revision, replace/mark stale records as a
transactional versioned snapshot rather than silently mixing revisions.

## Consequences

- Citations require source ID, source version/read marker, native locator and
  executed operation; a catalog node ID is optional for live query results.
- Native database rows and scientific arrays are retrieved in bounded operations
  and are not bulk-copied to the index or prompt.
- Schema evolution is explicit; duplicate IDs, malformed locators and conflicting
  source versions need negative tests.

## Alternatives rejected

- Embeddings/vector stores and fixed-token chunks are outside the v0.1 design.
- Treating full source text or generated summaries as the evidence store loses
  source authority and precise provenance.
