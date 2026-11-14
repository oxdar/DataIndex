# ADR-006: Public monorepo, private commercial repos and versioning

- **Status:** Proposed for Phase 00 review
- **Date:** 2026-11-13

## Context

The project needs practical shared development for public free packages while
ensuring extensions can be installed and released separately. Proprietary
implementations must not enter the public repository.

## Decision

Use one public repository for core and free extensions during v0.1 development,
with each package in its own buildable wheel boundary and release lifecycle.
Keep production commercial packages in private repositories, depending on the
public core contract. Public synthetic premium fixtures may test the same
contract but contain no proprietary implementation.

Version core distribution, each extension distribution and the extension API
independently. Extensions declare a PEP 440 core API range in static manifest;
compatibility is checked before module import. The initial proposed range is
`>=0.1,<0.2`.

## Consequences

- CI must build and install wheels in clean environments; editable imports are
  insufficient for release acceptance.
- Release metadata, hashes and dependency/license records are per distribution.
- A future move to separate public repositories can happen without changing
  runtime discovery, provided the contract remains stable.
- Package names and open-source license selection remain publication decisions.

## Alternatives rejected

- A single distribution prevents independent installation/versioning.
- Publishing premium source in a public monorepo violates the product boundary.
- Inferring API compatibility only from successful installation is unsafe.
