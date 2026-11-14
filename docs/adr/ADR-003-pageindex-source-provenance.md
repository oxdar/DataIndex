# ADR-003: Pinned PageIndex source review and narrow adaptation

- **Status:** Proposed for Phase 00 review
- **Date:** 2026-11-13

## Context

The build requirement is to inspect upstream PageIndex MIT source and adapt
useful tree-indexing/navigation code into DataIndex without a runtime
`pageindex` dependency. The DataIndex workspace is still in architecture phase.

## Decision

Use upstream snapshot
[`2ac8694bf8fe89a3840dda9bdb88db5119b77fc4`](https://github.com/VectifyAI/PageIndex/commit/2ac8694bf8fe89a3840dda9bdb88db5119b77fc4)
for the initial source review. The MIT license file states
`Copyright (c) 2025 Vectify AI`. The Phase 01 candidate is the ordered-stack
parent-assignment pattern in `pageindex/flash/outline/tree.py::build_tree`,
transformed to typed DataIndex nodes and locators. Do not port its PDF page
ranges, `Block` model, heading-style classification, page-cost optimization or
LLM expansion. Revalidate usefulness against real catalog code in Phase 01.

No code is copied during Phase 00. If code is derived in Phase 01, retain the
full MIT text and copyright notice and record exact upstream commit, source
function/file and transformation in `THIRD_PARTY_NOTICES.md`. Never import
PageIndex at runtime.

## Consequences

- This pins a reviewed source snapshot, not an assertion that a port exists.
- The candidate algorithm is small and must earn inclusion through typed
  hierarchy tests; if unnecessary, document why and do not copy it.
- The source review is not legal advice or blanket IP clearance.

## Alternatives rejected

- Porting PDF-specific tree optimization imports irrelevant concepts and
  dependencies into a format-neutral core.
- Depending on the PageIndex package violates the runtime boundary and couples
  release compatibility to upstream.
