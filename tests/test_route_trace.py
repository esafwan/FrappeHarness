import hashlib

from frappe_harness.assisted_flow import AssistedFlowCoordinator
from frappe_harness.monitor_adapter import MonitorAdapter
from frappe_harness.route_adapter import RouteClassifierAdapter
from frappe_harness.route_contracts import Route, RouteFact, RouteRequest
from frappe_harness.route_trace import parse_route_trace, replay_next_action, trace_digest, trace_from_outcome


def _request():
    return RouteRequest("req_001", "route_request", "spec_pending", hashlib.sha256(b"f").hexdigest(),
                        (RouteFact("entity_count", "1"),), (Route.SPEC_EXTRACT, Route.CLARIFY, Route.ESCALATE))


def test_trace_contains_only_normalized_fields_and_replays_same_action():
    coordinator = AssistedFlowCoordinator(
        RouteClassifierAdapter(lambda _: {"schema_version": 1, "request_id": "req_001", "decision_id": "dec_001", "route": "SPEC_EXTRACT", "reason_codes": [], "confidence": 1}),
        lambda *_: {"accepted": True},
        MonitorAdapter(lambda snapshot: {"schema_version": 1, "snapshot_id": snapshot.snapshot_id, "snapshot_digest": snapshot.snapshot_digest, "action": "CONTINUE", "reason_codes": []}),
    )
    trace = trace_from_outcome(coordinator.run(_request()))
    assert trace.trace_digest == trace_digest(trace)
    assert replay_next_action(trace) == "CONTINUE"
    assert parse_route_trace(trace.to_record()) == trace
