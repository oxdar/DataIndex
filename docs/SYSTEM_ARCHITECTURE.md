# DataIndex v0.1.0 — Greenfield architecture

This document describes the DataIndex v0.1 architecture.

## Boundaries and responsibilities

| Component | Responsibility | Explicit boundary |
|---|---|---|
| Client / SDK / CLI | Submit typed requests and render cited results | Never creates executable SQL or plugin code from model output |
| Source registry | Stable source ID, type, version/read marker and access policy reference | Does not copy or replace authoritative content |
| Indexer | Ask a compatible, trusted extension for typed index records | Indexes structure, metadata, relationships and native locators; not bulk source bodies |
| Catalog and graph | Persist versioned nodes, hierarchies, relationships and provenance | Does not treat summaries or inferred edges as primary evidence |
| Extension registry | Discover entry-point metadata; validate ID, API range, trust and enablement | Discovery is not import, trust or sandboxing |
| Tool dispatcher | Enforce identity, source authorization, plugin state, entitlement and budgets | Is the sole execution boundary for retrieval tools |
| Native extension | Read its source using bounded, deterministic operations | Cannot grant access, alter entitlement, or bypass dispatch |
| Optional planner | Select among predeclared schema-constrained tools | Cannot execute SQL, filesystem, network or Python supplied by an LLM |
| Evidence composer | Assemble results with source/version/locator/operation citations | Does not convert model-generated summaries into observed facts |

Core is a small Python distribution (`dataindex-core`, namespace `dataindex`) with
no extension, LLM-provider, database-driver, OCR, scientific or licensing-service
dependency at import time. SQLite is the local catalog store. Free and commercial
extensions are independent wheels implementing the same versioned contract.

## Source, index and evidence lifecycle

```mermaid
flowchart LR
  S[Authoritative source] --> R[Register source ID, type, access scope]
  R --> P[Trusted extension probes source]
  P --> I[Indexer reads structure and native metadata]
  I --> N[Typed nodes, relationships, locators]
  N --> C[(Versioned local SQLite catalog)]
  C --> Q[Candidate navigation]
  Q --> D[Central tool dispatcher]
  D --> X[Bounded native source operation]
  X --> E[Evidence with actual source version and locator]
  E --> A[Cited answer]
  S -. remains authoritative .-> X
```

The index is a navigational cache, not a source-of-truth copy. A document source
version is a content revision/hash or provider revision; a live database operation
records the strongest available read timestamp or transaction marker. Re-indexing
creates/replaces a source-version snapshot transactionally; stale catalog entries
must not silently stand in for a newer source revision. Source-specific locators
remain native (for example, a Markdown heading anchor or a database table/query
locator). No fixed-token segmentation or embeddings are used.

## Retrieval lifecycle

```mermaid
flowchart TD
  U[Requester and tenant] --> I[Authenticate identity]
  I --> SA[Authorize source and operation]
  SA --> PL[Resolve predeclared tool]
  PL --> ST[Check installed, discovered, compatible, trusted, enabled, healthy]
  ST --> EN{Premium capability?}
  EN -- no --> B[Apply timeout, quota and result bounds]
  EN -- yes --> EC[Check capability entitlement for tenant]
  EC --> B
  B --> T[Execute trusted extension tool]
  T --> V[Validate result and capture provenance]
  V --> C[Compose cited answer or abstain]
```

The dispatcher checks source authorization and commercial entitlement separately.
An entitlement only permits a capability; it never grants access to a source. The
optional planner can request only registered tool IDs and their declared input
schemas. SQL uses pre-approved operations, bound parameters, source/table/column
allowlists, read-only credentials and configured limits.

## Installation and plugin lifecycle

```mermaid
flowchart LR
  F[Free public wheel] --> ENV[Install into environment]
  M[Private commercial wheel] --> ENV
  ENV --> EP[Read dataindex.extensions entry-point metadata]
  EP --> MF[Read static extension manifest without importing plugin]
  MF --> UQ[Check unique ID and core API compatibility]
  UQ --> TR{Administrator trust rule matches?}
  TR -- no --> DENY[Report untrusted; do not load]
  TR -- yes --> LOAD[Load entry point]
  LOAD --> VM[Verify runtime manifest and protocol]
  VM --> ON[Administrator enables plugin]
  ON --> HC[Health check before use]
```

Entry-point discovery is not a sandbox. Only explicitly trusted code may be loaded
in-process. Unknown, incompatible, duplicate, unhealthy or disabled plugins fail
independently and must not disable unrelated extensions. The static manifest is
packaged with the extension so core can check its declared ID, edition, API range,
capabilities and source types before importing the entry point; after loading,
the runtime manifest must match it.

## Commercial activation

```mermaid
sequenceDiagram
  participant Caller
  participant Dispatcher
  participant SourcePolicy
  participant Registry
  participant Entitlements
  participant Extension
  Caller->>Dispatcher: tool request + tenant identity
  Dispatcher->>SourcePolicy: authorize source and operation
  SourcePolicy-->>Dispatcher: allow or deny
  Dispatcher->>Registry: resolve compatible, trusted, enabled, healthy tool
  Registry-->>Dispatcher: tool metadata
  Dispatcher->>Entitlements: check capability for tenant (premium only)
  Entitlements-->>Dispatcher: grant or deny
  Note over Dispatcher,Entitlements: A grant does not alter source authorization
  Dispatcher->>Extension: bounded request after all policy checks
  Extension-->>Dispatcher: value + source-backed evidence
  Dispatcher-->>Caller: cited result or explicit denial
```

For v0.1, the entitlement interface is provider-neutral and tested with a
deterministic fixture; it is not a billing or production license service. Hosted
deployments must enforce both checks server-side. Local checks are an operational
gate, not tamper-proof protection for proprietary code.

## Test/install matrix required by the design

| Environment | Required evidence |
|---|---|
| Core wheel only | Offline import, catalog and CLI work; no optional imports |
| Core + documents wheel | Markdown, TXT and HTML fixtures return native locators |
| Core + databases wheel | SQLite schema and approved bounded read operations work without driver extras; DuckDB and server engines work with their extras |
| Core + both free wheels | Reproducible mixed-source evidence path works |
| Core + synthetic premium wheel | Deny, grant and revoke are checked at execution |
| Remove one extension | Core and remaining extensions continue to work |
