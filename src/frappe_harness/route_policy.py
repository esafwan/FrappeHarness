"""Deterministic Python-owned route policy for the V1 Route enum.

This module maps a normalized ``(state, operation)`` pair to an explicit set of
allow-listed routes.  It has no model, execution, migration, tool, or approval
authority: it only decides whether a proposed route is permitted by the static
V1 transition table.  Every unknown state/operation and every route outside the
policy fails closed to ``ESCALATE`` with typed reason codes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from frappe_harness.route_contracts import Route, RouteContractError, RouteDecision, RouteRequest


class RoutePolicyError(ValueError):
    """The requested state/operation or route is outside the V1 policy."""


@dataclass(frozen=True)
class RoutePolicyOutcome:
    """Deterministic result of applying the V1 route policy.

    ``approved`` is ``True`` only when the proposed route is explicitly listed
    for the request's ``(state, operation)`` pair and is also present in the
    request's own ``allowed_routes``.  When ``approved`` is ``False`` the
    effective route is always ``Route.ESCALATE``.
    """

    approved: bool
    route: Route
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.approved, bool):
            raise RoutePolicyError("approved must be a boolean")
        if not isinstance(self.route, Route):
            raise RoutePolicyError("route must be an allow-listed Route")
        if not isinstance(self.reason_codes, tuple) or any(not isinstance(code, str) for code in self.reason_codes):
            raise RoutePolicyError("reason_codes must be an immutable tuple of strings")


# V1 transition table: each (state, operation) maps to the exact set of routes
# that deterministic Python will admit for that normalized boundary state.
_V1_ROUTE_POLICY: Mapping[tuple[str, str], frozenset[Route]] = {
    ("spec_pending", "route_request"): frozenset(
        {Route.SPEC_EXTRACT, Route.CLARIFY, Route.ESCALATE}
    ),
    ("spec_repair_pending", "repair_request"): frozenset(
        {Route.SPEC_REPAIR, Route.CLARIFY, Route.ESCALATE}
    ),
    ("modification_pending", "assess_request"): frozenset(
        {Route.MODIFICATION_ASSESS, Route.CLARIFY, Route.ESCALATE}
    ),
    ("explain_pending", "explain_request"): frozenset(
        {Route.FACTS_EXPLAIN, Route.CLARIFY, Route.ESCALATE}
    ),
    ("test_pending", "diagnose_request"): frozenset(
        {Route.TEST_DIAGNOSE, Route.CLARIFY, Route.ESCALATE}
    ),
    ("clarify_pending", "clarify_request"): frozenset(
        {Route.CLARIFY, Route.ESCALATE}
    ),
}


_V1_STATES: frozenset[str] = frozenset(key[0] for key in _V1_ROUTE_POLICY)
_V1_OPERATIONS: frozenset[str] = frozenset(key[1] for key in _V1_ROUTE_POLICY)


def v1_policy_routes(state: str, operation: str) -> frozenset[Route]:
    """Return the allow-listed routes for a normalized V1 state/operation pair.

    Raises ``RoutePolicyError`` for any state or operation that is not in the
    explicit V1 table.  This is the typed-rejection form of the fail-closed
    boundary; callers that prefer an ``ESCALATE`` outcome should use
    ``apply_route_policy`` instead.
    """

    if not isinstance(state, str) or not isinstance(operation, str):
        raise RoutePolicyError("state and operation must be strings")
    key = (state, operation)
    allowed = _V1_ROUTE_POLICY.get(key)
    if allowed is None:
        if state not in _V1_STATES and operation not in _V1_OPERATIONS:
            raise RoutePolicyError(f"unknown state and operation: {state}/{operation}")
        if state not in _V1_STATES:
            raise RoutePolicyError(f"unknown state: {state}")
        raise RoutePolicyError(f"unknown operation: {operation}")
    return allowed


def apply_route_policy(request: RouteRequest, decision: RouteDecision) -> RoutePolicyOutcome:
    """Apply the V1 route policy to a bound request/decision pair.

    The function returns a ``RoutePolicyOutcome``.  If the decision's route is
    not permitted by the policy for the request's ``(state, operation)`` pair,
    or if the pair itself is unknown, the outcome is ``approved=False`` with
    ``route=Route.ESCALATE``.  The function never mutates its inputs.
    """

    if not isinstance(request, RouteRequest):
        raise RoutePolicyError("request must be a RouteRequest")
    if not isinstance(decision, RouteDecision):
        raise RoutePolicyError("decision must be a RouteDecision")
    if decision.request_id != request.request_id:
        return RoutePolicyOutcome(
            approved=False,
            route=Route.ESCALATE,
            reason_codes=("stale_or_mismatched_request",),
        )

    try:
        allowed = v1_policy_routes(request.state, request.operation)
    except RoutePolicyError as exc:
        return RoutePolicyOutcome(
            approved=False,
            route=Route.ESCALATE,
            reason_codes=("unknown_state_operation", str(exc)),
        )

    if decision.route not in request.allowed_routes:
        return RoutePolicyOutcome(
            approved=False,
            route=Route.ESCALATE,
            reason_codes=("route_not_allowed_by_request",),
        )

    if decision.route not in allowed:
        return RoutePolicyOutcome(
            approved=False,
            route=Route.ESCALATE,
            reason_codes=("route_not_in_policy",),
        )

    return RoutePolicyOutcome(
        approved=True,
        route=decision.route,
        reason_codes=decision.reason_codes,
    )
