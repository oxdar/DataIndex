"""Provider-neutral entitlement decisions for centralized tool dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class EntitlementReason(str, Enum):
    GRANTED = "granted"
    NOT_FOUND = "not_found"
    EXPIRED = "expired"
    REVOKED = "revoked"
    TENANT_MISMATCH = "tenant_mismatch"
    DEPLOYMENT_MISMATCH = "deployment_mismatch"
    PROVIDER_UNAVAILABLE = "provider_unavailable"


@dataclass(frozen=True, slots=True)
class EntitlementDecision:
    granted: bool
    reason: EntitlementReason

    def __post_init__(self) -> None:
        if not isinstance(self.granted, bool):
            raise ValueError("granted must be a bool")
        if not isinstance(self.reason, EntitlementReason):
            raise ValueError("reason must be an EntitlementReason")
        if self.granted != (self.reason is EntitlementReason.GRANTED):
            raise ValueError("granted must match the entitlement decision reason")


class EntitlementProvider(Protocol):
    """Check a manifest entitlement ID for a tenant and deployment.

    ``capability`` is the entitlement ID mapped from the extension's declared
    tool capability; it is not the tool capability itself.
    """

    def check(
        self, tenant_id: str, capability: str, deployment: str
    ) -> EntitlementDecision: ...
