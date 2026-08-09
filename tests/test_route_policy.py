from __future__ import annotations

import dataclasses
import hashlib

import pytest

from frappe_harness.route_contracts import Route, RouteDecision, RouteFact, RouteRequest
from frappe_harness.route_policy import (
    RoutePolicyError,
    RoutePolicyOutcome,
    apply_route_policy,
    v1_policy_routes,
)


def _make_request(
    state: str,
    operation: str,
    allowed_routes: tuple[Route, ...],
    request_id: str = "req_001",
) -> RouteRequest:
    return RouteRequest(
        request_id=request_id,
        operation=operation,
        state=state,
        context_digest=hashlib.sha256(b"facts").hexdigest(),
        facts=(RouteFact("entity_count", "1"),),
        allowed_routes=allowed_routes,
    )


def _make_decision(route: Route, request_id: str = "req_001") -> RouteDecision:
    return RouteDecision(
        request_id=request_id,
        decision_id="dec_001",
        route=route,
        reason_codes=("policy_test",),
        confidence=0.9,
    )


# Table of every V1 state/operation pair and each route explicitly allowed.
_V1_TRANSITION_CASES: list[tuple[str, str, Route]] = [
    ("spec_pending", "route_request", Route.SPEC_EXTRACT),
    ("spec_pending", "route_request", Route.CLARIFY),
    ("spec_pending", "route_request", Route.ESCALATE),
    ("spec_repair_pending", "repair_request", Route.SPEC_REPAIR),
    ("spec_repair_pending", "repair_request", Route.CLARIFY),
    ("spec_repair_pending", "repair_request", Route.ESCALATE),
    ("modification_pending", "assess_request", Route.MODIFICATION_ASSESS),
    ("modification_pending", "assess_request", Route.CLARIFY),
    ("modification_pending", "assess_request", Route.ESCALATE),
    ("explain_pending", "explain_request", Route.FACTS_EXPLAIN),
    ("explain_pending", "explain_request", Route.CLARIFY),
    ("explain_pending", "explain_request", Route.ESCALATE),
    ("test_pending", "diagnose_request", Route.TEST_DIAGNOSE),
    ("test_pending", "diagnose_request", Route.CLARIFY),
    ("test_pending", "diagnose_request", Route.ESCALATE),
    ("clarify_pending", "clarify_request", Route.CLARIFY),
    ("clarify_pending", "clarify_request", Route.ESCALATE),
]


@pytest.mark.parametrize(("state", "operation", "route"), _V1_TRANSITION_CASES)
def test_policy_approves_every_explicitly_allowed_v1_route(
    state: str, operation: str, route: Route
) -> None:
    allowed = (route,) if route is not Route.ESCALATE else (Route.ESCALATE, Route.CLARIFY)
    request = _make_request(state, operation, allowed_routes=allowed)
    decision = _make_decision(route)
    outcome = apply_route_policy(request, decision)
    assert outcome.approved is True
    assert outcome.route is route
    assert outcome.reason_codes == ("policy_test",)


@pytest.mark.parametrize(("state", "operation", "route"), _V1_TRANSITION_CASES)
def test_lookup_returns_same_allowed_routes_as_policy(
    state: str, operation: str, route: Route
) -> None:
    allowed = v1_policy_routes(state, operation)
    assert route in allowed


def test_disallowed_route_in_request_fails_closed_to_escalate() -> None:
    request = _make_request(
        "spec_pending",
        "route_request",
        allowed_routes=(Route.SPEC_EXTRACT, Route.MODIFICATION_ASSESS, Route.ESCALATE),
    )
    decision = _make_decision(Route.MODIFICATION_ASSESS)
    outcome = apply_route_policy(request, decision)
    assert outcome.approved is False
    assert outcome.route is Route.ESCALATE
    assert "route_not_in_policy" in outcome.reason_codes


def test_route_not_allowed_by_request_fails_closed_to_escalate() -> None:
    request = _make_request(
        "spec_pending",
        "route_request",
        allowed_routes=(Route.SPEC_EXTRACT, Route.CLARIFY),
    )
    decision = _make_decision(Route.ESCALATE)
    outcome = apply_route_policy(request, decision)
    assert outcome.approved is False
    assert outcome.route is Route.ESCALATE
    assert "route_not_allowed_by_request" in outcome.reason_codes


@pytest.mark.parametrize(
    ("state", "operation", "expected_fragment"),
    [
        ("unknown_state", "route_request", "unknown state"),
        ("spec_pending", "unknown_operation", "unknown operation"),
        ("unknown_state", "unknown_operation", "unknown state and operation"),
    ],
)
def test_unknown_state_or_operation_fails_closed_to_escalate(
    state: str, operation: str, expected_fragment: str
) -> None:
    request = _make_request(
        state,
        operation,
        allowed_routes=tuple(Route),
    )
    decision = _make_decision(Route.SPEC_EXTRACT)
    outcome = apply_route_policy(request, decision)
    assert outcome.approved is False
    assert outcome.route is Route.ESCALATE
    assert "unknown_state_operation" in outcome.reason_codes

    with pytest.raises(RoutePolicyError, match=expected_fragment):
        v1_policy_routes(state, operation)


@pytest.mark.parametrize("route", list(Route))
def test_escalate_is_always_included_in_v1_policy_sets(route: Route) -> None:
    for allowed in _V1_TRANSITION_CASES:
        _, _, r = allowed
        if r is Route.ESCALATE:
            break
    else:
        pytest.fail("no ESCALATE case found")
    assert Route.ESCALATE in v1_policy_routes("spec_pending", "route_request")


def test_mismatched_request_id_fails_closed_to_escalate() -> None:
    request = _make_request("spec_pending", "route_request", allowed_routes=(Route.SPEC_EXTRACT,))
    decision = RouteDecision(
        request_id="req_other",
        decision_id="dec_001",
        route=Route.SPEC_EXTRACT,
    )
    outcome = apply_route_policy(request, decision)
    assert outcome.approved is False
    assert outcome.route is Route.ESCALATE
    assert "stale_or_mismatched_request" in outcome.reason_codes


def test_policy_rejects_untyped_inputs() -> None:
    request = _make_request("spec_pending", "route_request", allowed_routes=(Route.SPEC_EXTRACT,))
    decision = _make_decision(Route.SPEC_EXTRACT)
    with pytest.raises(RoutePolicyError, match="request must be a RouteRequest"):
        apply_route_policy("not-a-request", decision)  # type: ignore[arg-type]
    with pytest.raises(RoutePolicyError, match="decision must be a RouteDecision"):
        apply_route_policy(request, "not-a-decision")  # type: ignore[arg-type]
    with pytest.raises(RoutePolicyError, match="state and operation must be strings"):
        v1_policy_routes(123, "route_request")  # type: ignore[arg-type]


def test_outcome_and_reasons_are_immutable() -> None:
    outcome = RoutePolicyOutcome(approved=True, route=Route.SPEC_EXTRACT, reason_codes=("ok",))
    assert dataclasses.is_dataclass(outcome) and outcome.__dataclass_params__.frozen
    with pytest.raises(AttributeError):
        outcome.approved = False  # type: ignore[misc]
    with pytest.raises(RoutePolicyError, match="immutable tuple"):
        RoutePolicyOutcome(approved=True, route=Route.SPEC_EXTRACT, reason_codes=["ok"])  # type: ignore[arg-type]


def test_policy_approval_preserves_decision_reason_codes() -> None:
    request = _make_request(
        "test_pending",
        "diagnose_request",
        allowed_routes=(Route.TEST_DIAGNOSE, Route.CLARIFY, Route.ESCALATE),
    )
    decision = RouteDecision(
        request_id="req_001",
        decision_id="dec_001",
        route=Route.TEST_DIAGNOSE,
        reason_codes=("flaky_test", "needs_rerun"),
    )
    outcome = apply_route_policy(request, decision)
    assert outcome.approved is True
    assert outcome.route is Route.TEST_DIAGNOSE
    assert outcome.reason_codes == ("flaky_test", "needs_rerun")
