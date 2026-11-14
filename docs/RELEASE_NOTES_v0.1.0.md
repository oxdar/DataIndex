# DataIndex v0.1.0 — draft release notes

**Status: unreleased candidate; not approved for publication.**

This is the first greenfield DataIndex implementation. Candidate packages are
the independent core, free document and free SQLite extensions, plus a
synthetic premium contract-test wheel. The premium fixture is not a commercial
product.

The candidate includes typed hierarchical metadata and evidence, bounded
source-grounded retrieval, an explicit plugin trust registry, host-supplied
authorization/entitlement contracts, a Python SDK and a local document CLI.
Core has no embedding or vector-database dependency and does not import
extensions unless an operator explicitly trusts and enables one.

Not included: hosted authentication, plugin sandboxing, a production license
service, arbitrary SQL, PDF/OCR, or production BIM/CAD/Subsurface integrations.
The local CLI tenant label is not an authentication boundary.

**Release blockers:** earlier Phase 03–06 human review gates remain open; the
core/free-extension license choice and extension license grants are unresolved;
package names and rights must be rechecked immediately before any publication.
No release tag or upload is authorized by this draft.
