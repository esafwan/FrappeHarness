import hashlib

import pytest

from frappe_harness.repair_flow import BoundedRepairCoordinator, RepairContractError
from frappe_harness.route_contracts import Route, RouteFact, RouteRequest


def _request(allowed=(Route.SPEC_REPAIR, Route.CLARIFY, Route.ESCALATE)):
    return RouteRequest("req_001", "repair_request", "spec_repair_pending", hashlib.sha256(b"f").hexdigest(),
                        (RouteFact("issue_count", "1"),), tuple(allowed))


def test_one_repair_returns_typed_success():
    result = BoundedRepairCoordinator(lambda request, issues: {"accepted": True, "reason_codes": ["fixed"]}).repair(_request(), ("missing_field",))
    assert result.status == "REPAIRED" and result.accepted and result.attempt_count == 1


def test_clarification_escalates_without_second_attempt():
    calls = []
    result = BoundedRepairCoordinator(lambda request, issues: calls.append(1) or {"accepted": False, "clarification_questions": ["Which field?"]}).repair(_request(), ("ambiguous",))
    assert result.status == "CLARIFY" and result.attempt_count == 1 and len(calls) == 1


def test_repair_route_not_allowed_stops_before_caller():
    called = []
    result = BoundedRepairCoordinator(lambda *_: called.append(1) or {"accepted": True}).repair(_request((Route.CLARIFY,)), ("issue",))
    assert result.status == "ESCALATED" and called == []


def test_malformed_result_and_issue_input_fail_closed():
    with pytest.raises(RepairContractError, match="unknown fields"):
        BoundedRepairCoordinator(lambda *_: {"accepted": True, "command": "no"}).repair(_request(), ("issue",))
    with pytest.raises(RepairContractError, match="immutable"):
        BoundedRepairCoordinator(lambda *_: {"accepted": True}).repair(_request(), ["issue"])  # type: ignore[arg-type]
