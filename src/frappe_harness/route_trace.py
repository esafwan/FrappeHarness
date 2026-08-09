"""Normalized durable trace and replay boundary for assisted-flow decisions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from .assisted_flow import AssistedFlowOutcome
from .monitor_adapter import MonitorAction
from .route_contracts import Route


class TraceContractError(ValueError):
    pass


@dataclass(frozen=True)
class RouteTrace:
    request_id: str
    decision_id: str
    route: Route
    policy_approved: bool
    worker_accepted: bool | None
    monitor_action: MonitorAction | None
    watchdog_action: MonitorAction | None
    hard_stop: bool
    reason_codes: tuple[str, ...]
    trace_digest: str

    def to_record(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id, "decision_id": self.decision_id,
            "route": self.route.value, "policy_approved": self.policy_approved,
            "worker_accepted": self.worker_accepted,
            "monitor_action": self.monitor_action.value if self.monitor_action else None,
            "watchdog_action": self.watchdog_action.value if self.watchdog_action else None,
            "hard_stop": self.hard_stop, "reason_codes": list(self.reason_codes),
            "trace_digest": self.trace_digest,
        }


def _canonical_values(trace: RouteTrace) -> str:
    return json.dumps({
        "request_id": trace.request_id, "decision_id": trace.decision_id,
        "route": trace.route.value, "policy_approved": trace.policy_approved,
        "worker_accepted": trace.worker_accepted,
        "monitor_action": trace.monitor_action.value if trace.monitor_action else None,
        "watchdog_action": trace.watchdog_action.value if trace.watchdog_action else None,
        "hard_stop": trace.hard_stop, "reason_codes": list(trace.reason_codes),
    }, sort_keys=True, separators=(",", ":"))


def trace_digest(trace: RouteTrace) -> str:
    return hashlib.sha256(_canonical_values(trace).encode()).hexdigest()


def trace_from_outcome(outcome: AssistedFlowOutcome) -> RouteTrace:
    if outcome.snapshot is None:
        request_id = outcome.request_id
        decision_id = outcome.decision_id or "unbound"
    else:
        request_id = outcome.snapshot.request_id
        decision_id = outcome.snapshot.decision_id
    trace = RouteTrace(
        request_id=request_id,
        decision_id=decision_id,
        route=outcome.route,
        policy_approved=outcome.policy.approved,
        worker_accepted=outcome.worker.accepted if outcome.worker else None,
        monitor_action=outcome.recommendation.action if outcome.recommendation else None,
        watchdog_action=outcome.watchdog.action if outcome.watchdog else None,
        hard_stop=outcome.watchdog.hard_stop if outcome.watchdog else False,
        reason_codes=tuple(outcome.policy.reason_codes)
        + (outcome.worker.reason_codes if outcome.worker else ())
        + (outcome.watchdog.reason_codes if outcome.watchdog else ()),
        trace_digest="0" * 64,
    )
    return RouteTrace(**{**trace.__dict__, "trace_digest": trace_digest(trace)})


def replay_next_action(trace: RouteTrace) -> str:
    """Derive the same safe next action from normalized trace only."""
    if trace.hard_stop or trace.watchdog_action is MonitorAction.STOP:
        return "STOP"
    if not trace.policy_approved or trace.worker_accepted is False:
        return "ESCALATE"
    if trace.watchdog_action is not None:
        return trace.watchdog_action.value
    if trace.monitor_action is not None:
        return trace.monitor_action.value
    return "ESCALATE"


def parse_route_trace(raw: object) -> RouteTrace:
    if not isinstance(raw, Mapping):
        raise TraceContractError("route trace must be an object")
    keys = {"request_id", "decision_id", "route", "policy_approved", "worker_accepted", "monitor_action", "watchdog_action", "hard_stop", "reason_codes", "trace_digest"}
    if set(raw) != keys:
        raise TraceContractError("route trace has unknown or missing fields")
    try:
        route = Route(raw["route"])
        monitor = MonitorAction(raw["monitor_action"]) if raw["monitor_action"] is not None else None
        watchdog = MonitorAction(raw["watchdog_action"]) if raw["watchdog_action"] is not None else None
        trace = RouteTrace(raw["request_id"], raw["decision_id"], route, raw["policy_approved"], raw["worker_accepted"], monitor, watchdog, raw["hard_stop"], tuple(raw["reason_codes"]), raw["trace_digest"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TraceContractError("route trace is malformed") from exc
    if trace.trace_digest != trace_digest(trace):
        raise TraceContractError("route trace digest does not match")
    return trace
