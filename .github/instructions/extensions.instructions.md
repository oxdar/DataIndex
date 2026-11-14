---
applyTo: "packages/extensions/**,examples/extensions/**,tests/plugins/**"
---
# Extension rules
- Each extension has its own `pyproject.toml`, lock/constraint policy, test suite, changelog, license, README and wheel.
- Register via `[project.entry-points."dataindex.extensions"]` and a unique plugin ID.
- Treat free vs paid as licensing metadata + separate distribution; do not embed premium source in public core.
- Never make core import optional plugin code until it has passed compatibility and trust checks.
- Return typed evidence with source IDs, version, native locator, and actual observed values; handle cancellation/timeout.
- Format support means a tested extraction path with explicit limitations, not a planned reader.
