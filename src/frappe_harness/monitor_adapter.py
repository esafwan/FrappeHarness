"""Pure data-only adapter for an independent monitor recommendation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

from .monitor_contract import MonitorAction, MonitorContractError, MonitorSnapshot


class MonitorAdapterError(ValueError):
    """Raised when a monitor response is malformed or bound to stale state."""


_KEYS = frozenset({"schema_version", "snapshot_id", "snapshot_digest", "action", "reason_codes"})


@runtime_checkable
class MonitorModelCaller(Protocol):
    def __call__(self, snapshot: MonitorSnapshot) -> Mapping[str, Any]:
        """Return a JSON-shaped recommendation only."""


@dataclass(frozen=True)
class MonitorRecommendation:
    action: MonitorAction
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.action, MonitorAction):
            raise MonitorAdapterError("action must be a MonitorAction")
        if not isinstance(self.reason_codes, tuple) or len(self.reason_codes) > 10 or any(not isinstance(code, str) or not code.strip() for code in self.reason_codes):
            raise MonitorAdapterError("reason_codes must be an immutable tuple of strings")


def parse_monitor_recommendation(raw: object, snapshot: MonitorSnapshot) -> MonitorRecommendation:
    if not isinstance(raw, Mapping) or any(not isinstance(key, str) for key in raw):
        raise MonitorAdapterError("monitor recommendation must be an object")
    unknown = set(raw) - _KEYS
    if unknown:
        raise MonitorAdapterError("monitor recommendation has unknown fields")
    if raw.get("schema_version") != snapshot.schema_version:
        raise MonitorAdapterError("monitor recommendation schema is stale")
    if raw.get("snapshot_id") != snapshot.snapshot_id or raw.get("snapshot_digest") != snapshot.snapshot_digest:
        raise MonitorAdapterError("monitor recommendation is bound to a stale snapshot")
    try:
        action = MonitorAction(raw.get("action"))
    except (TypeError, ValueError) as exc:
        raise MonitorAdapterError("monitor action is not allow-listed") from exc
    reason_codes = raw.get("reason_codes", [])
    if not isinstance(reason_codes, list) or len(reason_codes) > 10:
        raise MonitorAdapterError("reason_codes must be a bounded array")
    if any(not isinstance(code, str) for code in reason_codes):
        raise MonitorAdapterError("reason_codes must contain strings")
    try:
        return MonitorRecommendation(action, tuple(reason_codes))
    except (TypeError, ValueError) as exc:
        raise MonitorAdapterError("monitor recommendation is malformed") from exc


class MonitorAdapter:
    def __init__(self, caller: MonitorModelCaller) -> None:
        self._caller = caller

    def recommend(self, snapshot: MonitorSnapshot) -> MonitorRecommendation:
        if not isinstance(snapshot, MonitorSnapshot):
            raise MonitorAdapterError("snapshot must be a MonitorSnapshot")
        try:
            return parse_monitor_recommendation(self._caller(snapshot), snapshot)
        except MonitorContractError as exc:
            raise MonitorAdapterError("monitor recommendation failed contract validation") from exc
