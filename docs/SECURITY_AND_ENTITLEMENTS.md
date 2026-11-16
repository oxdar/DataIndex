# Security, authorization and commercial entitlements

The implementation provides an in-process policy-enforcing dispatcher, bounded
reasoning, and a synthetic premium contract fixture. This is not a hosted
authentication, isolation or billing service.

## Separate predicates

`installed != discovered != compatible != trusted != enabled != healthy`.
For a premium operation, `entitled` is another independent state; `tenant_authorized`
for the requested source and operation is independent of every plugin/license state.
The dispatcher requires all applicable predicates on every execution.

## Central dispatch requirements

1. Authenticate the requester and establish tenant/deployment identity.
2. Resolve and authorize the requested source, data scope and operation; deny by
   default when policy is absent or ambiguous.
3. Resolve a declared tool from a compatible plugin that is explicitly trusted,
   enabled and healthy.
4. For commercial capabilities, check the mapped entitlement ID for the
   requester/tenant/deployment at execution time. A license does not authorize
   source access.
5. Apply per-tool time, call, row/result-size and resource budgets.
6. Validate parameters against the declared schema; execute only the native,
   bounded operation; validate evidence and provenance before returning it.
7. Audit decision and outcome without logging credentials, secrets or bulk source
   content. Surface denials and failures explicitly.

`ToolDispatcher` receives the host-established tenant and deployment IDs and
calls explicit source-authorization and optional `EntitlementProvider`
interfaces. A provider returns a typed decision with a stable denial reason;
the dispatcher includes that reason code in `EntitlementDeniedError` and logs
the code without source content or credentials. The test-only in-memory provider
also records decision events. Core does not provide durable audit storage.
The dispatcher denies missing/false authorization, checks the catalog's source
tenant, and rechecks plugin status and health on dispatch. `ReasoningBudget`
bounds reasoning depth, tool calls, model-reported tokens, result count and a
cooperative monotonic deadline. These local interfaces do not authenticate
requesters, provide per-tenant quota persistence, or force-stop a blocked
plugin or planner.

Hosted deployments must enforce source authorization and entitlement server-side.
Offline local checks are not tamper-proof and must not be marketed as protecting
distributed proprietary code. The v0.1 entitlement provider is a host-supplied
policy interface, not billing infrastructure. The synthetic premium wheel and
its in-memory test provider are contract fixtures only, not production
commercial code or a licensing service.

## Plugin trust and code execution

Python entry points execute arbitrary code when loaded. Static metadata discovery
must not import extension modules. Before load, require an administrator-configured
trust rule tied to the installed distribution identity/version and approved
artifact provenance. The core allowlist currently compares distribution
name and version; it does not verify wheel signatures or hashes, so installation
controls must establish artifact provenance. A publisher string in a manifest
is only a claim. Unknown or untrusted plugins remain unloaded. Trust and compatibility checks reduce
accidental/unauthorized loading; they do not sandbox a trusted plugin. Do not load
third-party plugin code into a privileged multi-tenant process without a separate
review and trust decision. Process isolation is future work, not a v0.1 claim.

## Local mode (single user)

`DataIndexClient(chat=...)` and the `submit`/`submit-db`/`documents`/`chat` CLI
commands serve one user on one machine. They change two defaults and weaken
nothing else; every tool call still passes through the central dispatcher.

- **Trust.** With `trusted_plugins=None` only the installed first-party free
  plugins (`dataindex-ext-documents`, `dataindex-ext-databases`) are trusted, by
  name and installed version. They still run in-process and are not sandboxed.
  Premium and third-party plugins load only when named in `trusted_plugins`
  (a name pins the installed version, a `(name, version)` pair pins an exact one);
  `trusted_plugins=[]` trusts none. This local-mode default does not apply to
  host deployments.
- **Source authorization.** Access is granted only to the files the user
  submitted, pinned to the source ID, type and version, and only for the
  capabilities of a free plugin plus `source.access`. A changed or missing file
  is refused until it is submitted again, and a different tenant is denied.
- **Entitlement.** Local mode has no `EntitlementProvider`, so commercial
  capabilities stay denied. A license never grants access to a source.
- **Model.** The model only returns JSON that selects offered tools and cites
  evidence it has been shown. `source_id` and `source_version` are injected by
  DataIndex and cannot be overridden; parameters are schema-validated; there is
  no model-authored SQL, shell, path or URL. An unknown tool or evidence ID gets
  one repair attempt, then the call fails. Answers carry source ID, version,
  native locator and operation; claims whose evidence was not retrieved are
  rejected. Claim text is still not proof of entailment.
- **Data sent to the provider.** Each planner call sends the question, document
  names, section outlines, tool schemas and the bounded evidence retrieved so
  far to the configured endpoint, never file paths or API keys. Use a loopback or
  private endpoint for confidential sources.
- **Secrets.** The API key comes only from `OPENAI_API_KEY`/`api_key` and is not
  stored in the catalog, workspace, prompts, logs or errors. A database server
  password comes only from the named `DATAINDEX_*` environment variable, read
  at each connection; a URL that contains a password is rejected, and only the
  variable name is stored. A database configuration saved in the workspace
  holds approved columns and queries, including fixed `source_filters` values
  in plaintext, so do not put secrets in it.

## Data and tool constraints

- The source remains authoritative; store typed catalog metadata and locators, not
  whole SQL tables, full scientific arrays or unrestricted document bodies.
- Treat source content, plugin output and model output as untrusted input.
- No model-authored arbitrary SQL, shell, Python, filesystem path or URL execution.
- SQL extensions require read-only database credentials plus operation/table/column
  allowlists, bound parameters, row/result limits, timeouts and source-level tenant
  filtering. Code checks do not replace database permissions. The databases
  extension's per-engine read-only session guards are defense in depth; on SQL
  Server, Snowflake, BigQuery and ClickHouse the login is the only barrier to
  writes.
- Every returned fact has source ID, source version/read marker, native locator and
  executed operation. Match catalog-backed locators to their nodes; non-catalog
  locators receive structural/provenance checks unless a host verifier is supplied.
  Do not accept citation IDs invented by an LLM.
- The core validates citation IDs and evidence provenance, not semantic entailment
  of claim text. Applications remain responsible for review or stronger claim
  verification before presenting high-impact answers.
- Distinguish observed, calculated and model-inferred data. Inferred relations are
  not proof; calculation outputs retain input/source provenance and units.
- Do not put secrets or raw source content into prompts, logs, catalogs or fixtures.

## Required negative tests before release

Test that missing/revoked entitlement prevents premium execution, while free
extensions continue; a valid entitlement with unauthorized source access still
denies; untrusted code is not imported; incompatible/duplicate plugins do not
disable others; SQL DML, unapproved tables and unbounded reads fail; and
document-originated instructions cannot override policy or fabricate citations.
