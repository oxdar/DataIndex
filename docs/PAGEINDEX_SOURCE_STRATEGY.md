# PageIndex source strategy — Phase 00 audit

The v0.1 requirement is to inspect pinned upstream MIT source and adapt useful
tree-indexing/navigation code directly into DataIndex, without importing
`pageindex` at runtime.

## Verified upstream snapshot

- Repository: <https://github.com/VectifyAI/PageIndex>
- Inspected default-branch commit: `2ac8694bf8fe89a3840dda9bdb88db5119b77fc4`
- Commit timestamp reported by GitHub: 2026-11-05 14:45:45 UTC
- License file at that commit: MIT, copyright `Copyright (c) 2025 Vectify AI`
- Inspected files: `pageindex/flash/outline/tree.py`,
  `pageindex/tree_optimize.py`, `tests/test_tree_format.py`, `README.md`,
  `LICENSE`, and `pyproject.toml`.

The source was fetched from raw GitHub URLs at the pinned commit. The tree
assembly implementation in `pageindex/flash/outline/tree.py` (notably
`build_tree`) uses an ordered heading stream and a stack to attach nodes to the
nearest preceding shallower heading. It also computes page ranges and prunes
empty child arrays. Its output and `Block`/PDF page model are PageIndex-specific.
`assign_levels` depends on PDF style/numbering metadata. `tree_optimize.py`
contains page-cost decisions and optional model-driven expansion. Those details
do not directly fit DataIndex's format-neutral native locators.

## Phase 01 adaptation

`packages/core/src/dataindex/hierarchy.py::build_hierarchy` adapts only the
ordered-stack parent-assignment pattern from `build_tree`. It accepts DataIndex
typed nodes and explicit levels, sets parent IDs, and preserves each native
locator and source version. It does not port PageIndex page-range fields, PDF
block/heading detection, style ranking, summary/LLM prompting, or optimization
policy. Focused DataIndex tests cover nested siblings, root transitions and
invalid source/version input. No upstream tests or PageIndex modules are imported.

The complete MIT text, copyright notice, pinned commit, source function and
transformation are recorded in `THIRD_PARTY_NOTICES.md`.

This source review is not a blanket legal/IP clearance. Review any future
transitive dependency and trademark use separately.
