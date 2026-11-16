# DataIndex extension protocol v1

**Status:** v0.1.0 typed models, metadata-first discovery, bounded reasoning,
centralized dispatch, and a synthetic premium contract fixture are implemented.
The host supplies identity and authorization/entitlement policies; provider
adapters remain optional.

## Packaging and safe discovery

Each extension is a separately versioned wheel with its own `pyproject.toml`,
tests, README, changelog, license and dependency policy. Register it in:

```toml
[project.entry-points."dataindex.extensions"]
documents = "dataindex_ext_documents.plugin:load_plugin"
```

The entry-point name is the unique plugin ID. Core first reads distribution and
entry-point metadata, then a static `dataindex-extension.json` packaged under the
entry-point's top-level module. That manifest includes the API range and declared
capabilities; it can be checked without importing the plugin module. Duplicate
IDs or missing/malformed manifests are reported per plugin. Only after a
configured trust rule and compatibility check may core load the entry point.
After load, the runtime manifest must match the static manifest. Entry-point
loading executes arbitrary Python and is never described as sandboxing.

## Versioned manifest

| Field | Type / invariant |
|---|---|
| `plugin_id` | Stable lowercase identifier; equals entry-point name |
| `distribution` | Installed wheel distribution name |
| `plugin_version` | PEP 440 distribution version |
| `core_api_range` | PEP 440 range against the public API version, initially `>=0.1,<0.2` |
| `edition` | `free` or `commercial` |
| `license_mode` | `none`, or a declared provider-independent entitlement mode |
| `capabilities` | Stable tool/index capability IDs; premium IDs have an entitlement key |
| `capability_entitlements` | Mapping of every commercial capability to its entitlement ID; empty for free plugins |
| `source_types` | MIME/source-type identifiers this extension can probe |
| `publisher` | Publisher claim; not proof of identity or trust |

The extension distribution version and core API version are distinct. Core rejects
an out-of-range API before loading. Additive compatible changes stay within an API
range; breaking changes require a new API range. Core never silently widens a
plugin's declared range.

## Public data model

Models are planned as Python 3.11+ standard-library dataclasses with explicit
construction-time validation. Wire/persisted values use JSON-compatible values
(`null`, booleans, integers, finite numbers, strings, arrays and string-keyed
objects); no Pydantic or provider-specific model is required by core.

| Model | Required fields and invariants |
|---|---|
| `SourceRef` | `source_id`, `source_type`, `version`; version is a revision/hash or an explicit live-read marker, never an invented timestamp |
| `NativeLocator` | `scheme`, `locator_version`, extension-defined JSON `value`; points into the authoritative source and contains no source body |
| `IndexRecord` | `node_id`, `source_id`, `source_version`, `kind`, optional `parent_id`, title, locator, typed attributes; optional summary is machine-generated metadata |
| `Relationship` | `relationship_id`, subject/object node IDs, predicate, evidence/provenance reference, relation kind (`observed`, `calculated` or `model_inferred`) and optional `identity_confidence` in `[0, 1]` (`null` means unmeasured) |
| `EvidenceResult` | `source_id`, `source_version`, optional `node_id`, locator, actual operation, retrieved value, evidence kind, execution ID and warnings |
| `RetrievalToolSpec` | Stable tool ID, JSON input schema, source/capability requirements, output limit and optional entitlement capability ID |
| `ToolRequest` | Registered tool ID and validated JSON parameters; never free-form executable code |
| `IndexContext` / `RetrievalContext` | Request/tenant reference, source scope, cancellation signal and resource budget; no entitlement or access grant is delegated to the extension |
| `HealthResult` | Explicit healthy/degraded/unavailable state and safe diagnostic; no secrets or source contents |
| `SourceAwareToolProvider` | Optional protocol for exposing source-specific, pre-approved tool schemas |

Every answerable evidence item must carry source ID, source version/read marker,
native locator and the operation actually executed. A `model_inferred`
relationship or summary is not factual evidence by itself. Database results may
have no catalog node; in that case `node_id` is absent, but the live source,
operation and native query/result locator remain required.

## Protocol and execution semantics

The proposed plugin interface is synchronous:

```python
class DataIndexExtension(Protocol):
    def manifest(self) -> ExtensionManifest: ...
    def probe(self, source: SourceRef) -> ProbeResult: ...
    def iter_index_records(
        self, source: SourceRef, ctx: IndexContext
    ) -> Iterator[IndexRecord]: ...
    def tools(self) -> Sequence[RetrievalToolSpec]: ...
    def run_tool(
        self, request: ToolRequest, ctx: RetrievalContext
    ) -> EvidenceResult: ...
    def health(self) -> HealthResult: ...
    def close(self) -> None: ...
```

Core owns persistence and dispatch; extensions return typed records and do not
write directly to the core store or policy state. Calls receive cooperative
monotonic deadlines and result budgets. Core rejects late or oversized results,
but cannot safely force-stop arbitrary in-process Python.
Hard termination/isolation is explicitly future work. An optional asynchronous
LLM adapter is not part of this extension ABI.

Tool input schemas use a bounded JSON Schema subset: `type`, object
`properties`/`required`/`additionalProperties`, `enum`, string and numeric
bounds, and array `items`/length bounds. Unsupported keywords fail closed.
Schemas are limited to 32 KiB and nesting depth 16.

An extension may implement `SourceAwareToolProvider.tools_for_source(source)`
in addition to `DataIndexExtension`. The dispatcher uses this to expose only
source-specific operations; the databases extension, for example, enumerates
approved query IDs for the registered database revision. Extensions without
this optional protocol continue to use `tools()`.

The core `ReasoningEngine` selects sources by bounded hierarchy metadata,
passes structured tool schemas to an optional provider-neutral `LLMPlanner`,
and requires each planned call to match an authorized source, loaded tool and
schema. It rechecks plugin trust/enablement/health, tenant/source/capability
authorization and any declared entitlement at dispatch. The host must provide
an authenticated tenant ID, deployment identifier and policy implementations;
core has no hosted identity service or entitlement backend.

Commercial calls use the typed `EntitlementProvider.check(tenant_id, capability,
deployment)` protocol. `capability` is the entitlement ID mapped from the
extension's declared tool capability. Providers return an `EntitlementDecision`
with a stable reason code (`not_found`, `expired`, `revoked`,
`tenant_mismatch`, `deployment_mismatch`, or `provider_unavailable`) on denial.
The dispatcher rechecks this decision for each call and carries the reason code
on `EntitlementDeniedError`; free capabilities do not consult an entitlement
provider. The synthetic in-memory test provider records decisions, but core
does not provide persistent audit storage or a production licensing backend.

Tool evidence must match the selected source revision and executed operation.
If it names a catalog node, its locator must match that node. Evidence without
a catalog node (such as an aggregate SQL result) is checked structurally and
for source/version/operation provenance; core cannot independently re-read that
source unless the host supplies a suitable dereferencer. Claim citations must
refer to returned evidence IDs, but core does not verify natural-language
entailment between claim text and cited content.

## Independent lifecycle and failure behavior

Keep these statuses separately observable: `installed`, `discovered`,
`compatible`, `trusted`, `enabled`, `entitled`, `tenant_authorized`, and
`healthy`. A status is not a substitute for another. Entitlement and source
authorization are rechecked at tool execution; a listing/status call does not
grant execution.

Use explicit errors for malformed manifests, duplicate IDs, incompatible API,
untrusted or disabled plugins, missing tools, invalid parameters, denied source
access, denied entitlement, cancellation, timeout, source-version change and
plugin execution failure. A failed plugin is isolated in registry reporting; it
does not become a successful empty result and does not disable unrelated plugins.

## Contract test matrix

| Test installation | Positive case | Negative / isolation case |
|---|---|---|
| Core only | Import and list zero plugins | Import does not load optional modules |
| Core + one free extension | Discover, trust, enable and call it | Uninstalling it leaves core usable |
| Core + two free extensions | Both provide independent tools | Duplicate ID disables only conflicting ID |
| Core + incompatible/malformed extension | Report exact incompatibility/manifest error | Entry-point code is not loaded |
| Core + untrusted extension | Metadata is visible as untrusted | Plugin module is not imported |
| Core + synthetic premium wheel | Entitled request executes | Missing, expired, revoked or cross-tenant access is denied; free tools remain usable |

Wheel-based isolated installs and `pip check` are required for implementation
validation; editable installs alone do not prove this contract.
