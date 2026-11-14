# ADR-001: Open core with independently installable plugins

- **Status:** Proposed for Phase 00 review
- **Date:** 2026-11-13

## Context

DataIndex needs a free offline-capable core and independently distributed free
and commercial format capabilities. Importing extensions from core would make
the smallest install depend on optional or proprietary code and could make plugin
failure destabilize core.

## Decision

Publish `dataindex-core` as the Python distribution exporting `dataindex`.
Package free documents and database connectors as independent wheels in the
public repository; keep production commercial implementations in private
repositories. All extensions use the same typed API and
`dataindex.extensions` entry-point group. Core has no unconditional extension
imports and works with zero extensions, LLM credentials or network access.

Entry-point metadata and static manifests are inspected before loading module
code. Load only an explicitly trusted, compatible plugin. Entry points do not
provide process isolation or sandboxing.

## Consequences

- Each distribution has its own version, dependencies, tests, build and release.
- Core-only, individual-plugin, mixed-source and premium-fixture installs are
  separate CI cases.
- Public premium tests may use only synthetic fixtures, not production
  commercial implementation.
- Proposed distribution-name availability must be rechecked before publication.

## Alternatives rejected

- A single megawheel couples install and release lifecycles.
- Optional imports in core make a minimal offline install fragile.
- Calling Python entry points a sandbox falsely implies isolation.
