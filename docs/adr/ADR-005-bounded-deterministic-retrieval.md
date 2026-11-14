# ADR-005: Deterministic retrieval with bounded optional reasoning

- **Status:** Proposed for Phase 00 review
- **Date:** 2026-11-13

## Context

The framework may use an LLM to navigate complex catalogs, but factual results
must come from native source operations and citations. Models and documents are
untrusted; generated code or unconstrained queries are not an acceptable tool
interface.

## Decision

Use deterministic catalog navigation and predeclared typed retrieval tools as
the baseline. An optional provider-neutral planner may select among registered
tools using schema-constrained parameters and explicit call/time/result budgets.
The dispatcher authorizes every call; native extensions perform bounded
read-only operations and return actual values with source/version/locator/
operation provenance. Answer composition labels observed, calculated and
model-inferred content and abstains when evidence is unavailable.

No embeddings, vector database, fixed-token chunking, model-authored arbitrary
SQL, shell, filesystem, network or Python operations are part of v0.1.

## Consequences

- Deterministic retrieval works without an LLM provider or credential.
- Numerical/database calculations remain in source tools; LLM prose cannot
  substitute for results.
- Prompt injection, hallucinated locators, unbounded tool loops and no-source
  abstention need negative tests.
- Planner provider/API and retrieval quality remain unproven until Phase 05
  representative end-to-end fixtures.

## Alternatives rejected

- Mandatory LLM calls break offline core use and make facts provider-dependent.
- Free-form generated SQL/code bypasses authorization and resource limits.
- Vector similarity and arbitrary token chunks violate the v0.1 product boundary.
