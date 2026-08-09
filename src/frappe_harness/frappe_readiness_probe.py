"""Typed, read-only Frappe readiness facts from the closed REST provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .environment_preflight import HealthCheck
from .frappe_rest_provider import FrappeRestProvider
from .run_store import LockTarget


class FrappeReadinessProbeError(RuntimeError):
    """The fixed REST readiness observation was unavailable or malformed."""


@dataclass(frozen=True)
class FrappeReadinessFacts:
    target: LockTarget
    health_checks: tuple[HealthCheck, ...]


class FrappeReadinessProbe:
    """Prove site and database-backed metadata reachability without inference.

    ``site`` passes only when the closed REST provider returns a successful
    typed response. ``database`` passes only when the response contains the
    requested DocType metadata object and its fields list. No SQL, arbitrary
    method path, or production designation is introduced.
    """

    def __init__(self, provider: FrappeRestProvider, *, doctype: str = "Task") -> None:
        if not isinstance(provider, FrappeRestProvider):
            raise ValueError("provider must be a FrappeRestProvider")
        self.provider = provider
        self.doctype = doctype

    def inspect(self, target: LockTarget) -> FrappeReadinessFacts:
        if not isinstance(target, LockTarget):
            raise ValueError("target must be a LockTarget")
        result = self.provider.inspect_meta(self.doctype)
        if not result.ok or not isinstance(result.payload, Mapping):
            raise FrappeReadinessProbeError("typed Frappe metadata readiness query failed")
        data = result.payload.get("data", result.payload)
        if not isinstance(data, Mapping) or not isinstance(data.get("fields"), list):
            raise FrappeReadinessProbeError("metadata readiness response has no typed fields list")
        reference = result.receipt.response_sha256
        return FrappeReadinessFacts(
            target,
            (
                HealthCheck("site", True, f"typed REST metadata response {reference}"),
                HealthCheck("database", True, f"metadata query returned fields {reference}"),
            ),
        )
