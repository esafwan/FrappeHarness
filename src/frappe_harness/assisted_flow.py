"""Deterministic composition of route, worker, monitor, and watchdog stages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

from .monitor_adapter import MonitorAdapter, MonitorRecommendation
from .monitor_contract import MonitorFact, MonitorSnapshot, build_monitor_snapshot
from .route_adapter import RouteClassifierAdapter
from .route_contracts import Route, RouteDecision, RouteRequest
from .route_policy import RoutePolicyOutcome, apply_route_policy
from .watchdog import WatchdogOutcome, evaluate_watchdog


class FlowContractError(ValueError):
    pass


@dataclass(frozen=True)
class WorkerResult:
    accepted: bool
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool):
            raise FlowContractError("worker accepted must be boolean")
        if not isinstance(self.reason_codes, tuple) or any(not isinstance(code, str) for code in self.reason_codes):
            raise FlowContractError("worker reason_codes must be an immutable tuple")


@runtime_checkable
class RouteWorker(Protocol):
    def __call__(self, request: RouteRequest, decision: RouteDecision) -> Mapping[str, Any]:
        """Return only a typed worker result mapping."""


def parse_worker_result(raw: object) -> WorkerResult:
    if not isinstance(raw, Mapping) or any(not isinstance(key, str) for key in raw):
        raise FlowContractError("worker result must be an object")
    unknown = set(raw) - {"accepted", "reason_codes"}
    if unknown:
        raise FlowContractError("worker result has unknown fields")
    if not isinstance(raw.get("accepted"), bool):
        raise FlowContractError("worker accepted must be boolean")
    reasons = raw.get("reason_codes", [])
    if not isinstance(reasons, list) or len(reasons) > 10 or any(not isinstance(code, str) for code in reasons):
        raise FlowContractError("worker reason_codes must be a bounded array")
    return WorkerResult(raw["accepted"], tuple(reasons))


@dataclass(frozen=True)
class AssistedFlowOutcome:
    request_id: str
    decision_id: str | None
    phase: str
    route: Route
    policy: RoutePolicyOutcome
    worker: WorkerResult | None
    snapshot: MonitorSnapshot | None
    recommendation: MonitorRecommendation | None
    watchdog: WatchdogOutcome | None


class AssistedFlowCoordinator:
    """Run the bounded data-only flow; no method here executes a provider."""

    def __init__(self, classifier: RouteClassifierAdapter, worker: RouteWorker, monitor: MonitorAdapter) -> None:
        self.classifier = classifier
        self.worker = worker
        self.monitor = monitor

    def run(self, request: RouteRequest, *, mutation_pending: bool = False) -> AssistedFlowOutcome:
        decision = self.classifier.classify(request)
        policy = apply_route_policy(request, decision)
        if not policy.approved:
            return AssistedFlowOutcome(request.request_id, decision.decision_id, "ESCALATED", policy.route, policy, None, None, None, None)

        worker = parse_worker_result(self.worker(request, decision))
        if not worker.accepted:
            return AssistedFlowOutcome(request.request_id, decision.decision_id, "WORKER_REJECTED", decision.route, policy, worker, None, None, None)

        snapshot = build_monitor_snapshot(
            snapshot_id=f"snap_{decision.decision_id}", request_id=request.request_id,
            decision_id=decision.decision_id, state=request.state, operation=request.operation,
            route=decision.route,
            facts=tuple(MonitorFact(item.key, item.value) for item in request.facts),
            budget_remaining=1, approval_present=False, lock_held=False,
            checkpoint_present=False,
        )
        recommendation = self.monitor.recommend(snapshot)
        watchdog = evaluate_watchdog(snapshot, recommendation, mutation_pending=mutation_pending)
        phase = "STOPPED" if watchdog.hard_stop or watchdog.action.value in {"STOP", "ESCALATE"} else "MONITORED"
        return AssistedFlowOutcome(request.request_id, decision.decision_id, phase, decision.route, policy, worker, snapshot, recommendation, watchdog)
