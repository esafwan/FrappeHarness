"""Pure data-only classifier adapter boundary.

The adapter sits between the deterministic Python control plane and an external
route model.  It accepts a validated `RouteRequest`, invokes a model caller
through a narrow Protocol, and returns exactly one validated `RouteDecision`.
This module deliberately imports no model client, subprocess, filesystem,
network, Frappe, credential, or tool capability.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

from frappe_harness.route_contracts import (
    RouteContractError,
    RouteDecision,
    RouteRequest,
    parse_route_decision,
)


class RouteAdapterError(ValueError):
    """Raised when the classifier adapter cannot produce a valid RouteDecision."""


@runtime_checkable
class RouteModelCaller(Protocol):
    """External model caller that returns a raw route decision mapping.

    Implementations must be pure-data: they receive an immutable
    `RouteRequest` and must return a JSON-shaped mapping that
    `parse_route_decision` can validate and bind to that request.
    """

    def __call__(self, request: RouteRequest) -> Mapping[str, Any]:
        """Return a raw decision mapping for the supplied request."""


class RouteClassifierAdapter:
    """Data-only adapter that binds one request to one typed model decision."""

    def __init__(self, caller: RouteModelCaller) -> None:
        self._caller = caller

    def classify(self, request: RouteRequest) -> RouteDecision:
        """Return a validated RouteDecision for the supplied request.

        The model caller is invoked with the request; its output is parsed
        and bound through `route_contracts.parse_route_decision`.  Any
        malformed, stale, or disallowed decision fails closed.
        """
        if not isinstance(request, RouteRequest):
            raise RouteAdapterError("request must be a RouteRequest")
        raw_decision = self._caller(request)
        try:
            return parse_route_decision(raw_decision, request)
        except RouteContractError as exc:
            raise RouteAdapterError("decision failed contract validation") from exc
