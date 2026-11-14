"""Versioned, provider-neutral public models for DataIndex extensions."""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import NoneType
from typing import Protocol, TypeAlias, runtime_checkable

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version


JsonScalar: TypeAlias = NoneType | bool | int | float | str
JsonValue: TypeAlias = (
    JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
)


class EvidenceKind(str, Enum):
    OBSERVED = "observed"
    CALCULATED = "calculated"
    MODEL_INFERRED = "model_inferred"


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _validate_json(value: object, path: str = "value") -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must not contain a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json(item, f"{path}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} object keys must be strings")
            _validate_json(item, f"{path}.{key}")
        return
    raise ValueError(f"{path} must contain only JSON-compatible values")


@dataclass(frozen=True, slots=True)
class SourceRef:
    source_id: str
    source_type: str
    version: str

    def __post_init__(self) -> None:
        _require_text("source_id", self.source_id)
        _require_text("source_type", self.source_type)
        _require_text("version", self.version)


@dataclass(frozen=True, slots=True)
class NativeLocator:
    scheme: str
    locator_version: int
    value: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        _require_text("scheme", self.scheme)
        if (
            isinstance(self.locator_version, bool)
            or not isinstance(self.locator_version, int)
            or self.locator_version < 1
        ):
            raise ValueError("locator_version must be a positive integer")
        if not isinstance(self.value, Mapping):
            raise ValueError("locator value must be an object")
        _validate_json(self.value)


@dataclass(frozen=True, slots=True)
class Node:
    node_id: str
    source_id: str
    source_version: str
    kind: str
    title: str
    locator: NativeLocator
    parent_id: str | None = None
    attributes: Mapping[str, JsonValue] = field(default_factory=dict)
    summary: str | None = None

    def __post_init__(self) -> None:
        for name in ("node_id", "source_id", "source_version", "kind", "title"):
            _require_text(name, getattr(self, name))
        if not isinstance(self.locator, NativeLocator):
            raise ValueError("locator must be a NativeLocator")
        if self.parent_id is not None:
            _require_text("parent_id", self.parent_id)
        if self.summary is not None and not isinstance(self.summary, str):
            raise ValueError("summary must be a string or None")
        if not isinstance(self.attributes, Mapping):
            raise ValueError("attributes must be an object")
        _validate_json(self.attributes, "attributes")


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    source_id: str
    source_version: str
    locator: NativeLocator
    operation: str
    value: JsonValue
    evidence_kind: EvidenceKind
    execution_id: str
    node_id: str | None = None
    unit: str | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("source_id", "source_version", "operation", "execution_id"):
            _require_text(name, getattr(self, name))
        if self.node_id is not None:
            _require_text("node_id", self.node_id)
        if self.unit is not None:
            _require_text("unit", self.unit)
        if not isinstance(self.evidence_kind, EvidenceKind):
            raise ValueError("evidence_kind must be an EvidenceKind")
        if not isinstance(self.locator, NativeLocator):
            raise ValueError("locator must be a NativeLocator")
        _validate_json(self.value)
        if not all(isinstance(warning, str) for warning in self.warnings):
            raise ValueError("warnings must contain only strings")


@dataclass(frozen=True, slots=True)
class Relationship:
    relationship_id: str
    subject_node_id: str
    predicate: str
    object_node_id: str
    evidence_ids: tuple[str, ...]
    evidence_kind: EvidenceKind
    identity_confidence: float | None = None

    def __post_init__(self) -> None:
        for name in (
            "relationship_id",
            "subject_node_id",
            "predicate",
            "object_node_id",
        ):
            _require_text(name, getattr(self, name))
        if not self.evidence_ids or not all(
            isinstance(evidence_id, str) and evidence_id.strip()
            for evidence_id in self.evidence_ids
        ):
            raise ValueError("relationships require non-empty evidence IDs")
        if not isinstance(self.evidence_kind, EvidenceKind):
            raise ValueError("evidence_kind must be an EvidenceKind")
        if self.identity_confidence is not None and (
            isinstance(self.identity_confidence, bool)
            or not isinstance(self.identity_confidence, (int, float))
            or not math.isfinite(self.identity_confidence)
            or not 0 <= self.identity_confidence <= 1
        ):
            raise ValueError("identity_confidence must be between 0 and 1 or None")


@dataclass(frozen=True, slots=True)
class ExtensionManifest:
    plugin_id: str
    distribution: str
    plugin_version: str
    core_api_range: str
    edition: str
    license_mode: str
    capabilities: tuple[str, ...]
    source_types: tuple[str, ...]
    publisher: str
    capability_entitlements: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "plugin_id",
            "distribution",
            "plugin_version",
            "core_api_range",
            "edition",
            "license_mode",
            "publisher",
        ):
            _require_text(name, getattr(self, name))
        if self.edition not in {"free", "commercial"}:
            raise ValueError("edition must be 'free' or 'commercial'")
        if self.license_mode not in {"none", "entitlement"}:
            raise ValueError("license_mode must be 'none' or 'entitlement'")
        if self.edition == "free" and self.license_mode != "none":
            raise ValueError("free plugins must use license_mode='none'")
        if self.edition == "commercial" and self.license_mode == "none":
            raise ValueError("commercial plugins must declare entitlement mode")
        for field_name in ("capabilities", "source_types"):
            values = getattr(self, field_name)
            if not isinstance(values, tuple) or not values or not all(
                isinstance(value, str) and value.strip() for value in values
            ):
                raise ValueError(f"{field_name} must be a tuple of non-empty strings")
        if not isinstance(self.capability_entitlements, Mapping) or not all(
            isinstance(capability, str)
            and capability in self.capabilities
            and isinstance(entitlement, str)
            and entitlement.strip()
            for capability, entitlement in self.capability_entitlements.items()
        ):
            raise ValueError("capability_entitlements must map declared capabilities to IDs")
        if self.edition == "commercial" and set(
            self.capability_entitlements
        ) != set(self.capabilities):
            raise ValueError("commercial capabilities must each declare an entitlement ID")
        if self.edition == "free" and self.capability_entitlements:
            raise ValueError("free plugins cannot declare entitlement-gated capabilities")
        try:
            Version(self.plugin_version)
            SpecifierSet(self.core_api_range)
        except (InvalidVersion, InvalidSpecifier) as error:
            raise ValueError(f"invalid plugin version or core API range: {error}") from error

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> ExtensionManifest:
        required = {
            "plugin_id",
            "distribution",
            "plugin_version",
            "core_api_range",
            "edition",
            "license_mode",
            "capabilities",
            "source_types",
            "publisher",
        }
        optional = {"capability_entitlements"}
        unknown = set(data) - required - optional
        missing = required - set(data)
        if missing or unknown:
            raise ValueError(
                f"manifest fields missing={sorted(missing)} unknown={sorted(unknown)}"
            )
        sequence_fields = ("capabilities", "source_types")
        if any(
            not isinstance(data[name], list)
            or not all(isinstance(value, str) for value in data[name])
            for name in sequence_fields
        ):
            raise ValueError("capabilities and source_types must be string arrays")
        text_fields = required - set(sequence_fields)
        if any(not isinstance(data[name], str) for name in text_fields):
            raise ValueError("manifest scalar fields must be strings")
        entitlements = data.get("capability_entitlements", {})
        if not isinstance(entitlements, Mapping) or not all(
            isinstance(capability, str) and isinstance(entitlement, str)
            for capability, entitlement in entitlements.items()
        ):
            raise ValueError("capability_entitlements must be a string mapping")
        return cls(
            plugin_id=data["plugin_id"],
            distribution=data["distribution"],
            plugin_version=data["plugin_version"],
            core_api_range=data["core_api_range"],
            edition=data["edition"],
            license_mode=data["license_mode"],
            capabilities=tuple(data["capabilities"]),
            source_types=tuple(data["source_types"]),
            publisher=data["publisher"],
            capability_entitlements=dict(entitlements),
        )

    def supports_core_api(self, core_api_version: str) -> bool:
        try:
            return SpecifierSet(self.core_api_range).contains(
                Version(core_api_version), prereleases=True
            )
        except (InvalidSpecifier, InvalidVersion):
            return False


@dataclass(frozen=True, slots=True)
class ProbeResult:
    supported: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class IndexContext:
    deadline_monotonic: float | None = None


@dataclass(frozen=True, slots=True)
class RetrievalContext:
    deadline_monotonic: float | None = None
    maximum_results: int = 100

    def __post_init__(self) -> None:
        if (
            isinstance(self.maximum_results, bool)
            or not isinstance(self.maximum_results, int)
            or self.maximum_results < 1
        ):
            raise ValueError("maximum_results must be positive")


@dataclass(frozen=True, slots=True)
class RetrievalToolSpec:
    tool_id: str
    input_schema: Mapping[str, JsonValue]
    capability: str
    maximum_results: int
    entitlement_capability: str | None = None

    def __post_init__(self) -> None:
        _require_text("tool_id", self.tool_id)
        _require_text("capability", self.capability)
        if (
            isinstance(self.maximum_results, bool)
            or not isinstance(self.maximum_results, int)
            or self.maximum_results < 1
        ):
            raise ValueError("maximum_results must be positive")
        if self.entitlement_capability is not None:
            _require_text("entitlement_capability", self.entitlement_capability)
        if not isinstance(self.input_schema, Mapping):
            raise ValueError("input_schema must be an object")
        _validate_json(self.input_schema, "input_schema")


@runtime_checkable
class SourceAwareToolProvider(Protocol):
    """Optional extension capability for source-specific tool schemas."""

    def tools_for_source(
        self, source: SourceRef
    ) -> Sequence[RetrievalToolSpec]: ...


@dataclass(frozen=True, slots=True)
class ToolRequest:
    tool_id: str
    parameters: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text("tool_id", self.tool_id)
        if not isinstance(self.parameters, Mapping):
            raise ValueError("parameters must be an object")
        _validate_json(self.parameters, "parameters")


@dataclass(frozen=True, slots=True)
class HealthResult:
    status: HealthStatus
    diagnostic: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, HealthStatus):
            raise ValueError("status must be a HealthStatus")
        if self.diagnostic is not None and not isinstance(self.diagnostic, str):
            raise ValueError("diagnostic must be a string or None")


@runtime_checkable
class DataIndexExtension(Protocol):
    def manifest(self) -> ExtensionManifest: ...

    def probe(self, source: SourceRef) -> ProbeResult: ...

    def iter_index_records(
        self, source: SourceRef, ctx: IndexContext
    ) -> Iterator[Node]: ...

    def tools(self) -> Sequence[RetrievalToolSpec]: ...

    def run_tool(
        self, request: ToolRequest, ctx: RetrievalContext
    ) -> EvidenceRecord: ...

    def health(self) -> HealthResult: ...

    def close(self) -> None: ...
