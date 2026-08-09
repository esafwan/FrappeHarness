import hashlib

import pytest

from frappe_harness.assisted_flow import AssistedFlowCoordinator, FlowContractError
from frappe_harness.monitor_adapter import MonitorAction, MonitorAdapter
from frappe_harness.route_adapter import RouteClassifierAdapter
from frappe_harness.route_contracts import Route, RouteFact, RouteRequest


def _request(state="spec_pending", operation="route_request"):
    return RouteRequest("req_001", operation, state, hashlib.sha256(b"facts").hexdigest(),
                        (RouteFact("entity_count", "1"),),
                        (Route.SPEC_EXTRACT, Route.MODIFICATION_ASSESS, Route.CLARIFY, Route.ESCALATE))


def _decision(_):
    return {"schema_version": 1, "request_id": "req_001", "decision_id": "dec_001",
            "route": "SPEC_EXTRACT", "reason_codes": ["complete"], "confidence": 0.9}


def _monitor(snapshot):
    return {"schema_version": 1, "snapshot_id": snapshot.snapshot_id,
            "snapshot_digest": snapshot.snapshot_digest, "action": "CONTINUE", "reason_codes": ["ok"]}


def test_composes_route_policy_worker_monitor_and_watchdog():
    calls = []
    coordinator = AssistedFlowCoordinator(
        RouteClassifierAdapter(_decision),
        lambda request, decision: {"accepted": True, "reason_codes": ["typed"]},
        MonitorAdapter(_monitor),
    )
    result = coordinator.run(_request())
    assert result.phase == "MONITORED"
    assert result.route is Route.SPEC_EXTRACT
    assert result.watchdog is not None and result.watchdog.action is MonitorAction.CONTINUE
    assert result.watchdog.hard_stop is False


def test_policy_escalation_stops_before_worker_or_monitor():
    called = {"worker": False, "monitor": False}
    coordinator = AssistedFlowCoordinator(
        RouteClassifierAdapter(lambda _: {**_decision(None), "route": "MODIFICATION_ASSESS"}),
        lambda request, decision: called.__setitem__("worker", True) or {"accepted": True},
        MonitorAdapter(lambda snapshot: called.__setitem__("monitor", True) or _monitor(snapshot)),
    )
    result = coordinator.run(_request())
    assert result.phase == "ESCALATED"
    assert called == {"worker": False, "monitor": False}


def test_worker_rejection_stops_before_monitor():
    coordinator = AssistedFlowCoordinator(
        RouteClassifierAdapter(_decision),
        lambda request, decision: {"accepted": False, "reason_codes": ["needs_clarification"]},
        MonitorAdapter(_monitor),
    )
    result = coordinator.run(_request())
    assert result.phase == "WORKER_REJECTED"
    assert result.snapshot is None


def test_mutation_pending_without_prerequisites_is_watchdog_stop():
    coordinator = AssistedFlowCoordinator(
        RouteClassifierAdapter(_decision),
        lambda request, decision: {"accepted": True},
        MonitorAdapter(_monitor),
    )
    result = coordinator.run(_request(), mutation_pending=True)
    assert result.phase == "STOPPED"
    assert result.watchdog is not None and result.watchdog.hard_stop
    assert "approval_missing" in result.watchdog.reason_codes


def test_worker_unknown_fields_fail_closed():
    coordinator = AssistedFlowCoordinator(
        RouteClassifierAdapter(_decision),
        lambda request, decision: {"accepted": True, "command": "never"},
        MonitorAdapter(_monitor),
    )
    with pytest.raises(FlowContractError, match="unknown"):
        coordinator.run(_request())
