import ast
import hashlib

import pytest

from frappe_harness.route_adapter import RouteAdapterError, RouteClassifierAdapter
from frappe_harness.route_contracts import Route, RouteFact, RouteRequest


def _request() -> RouteRequest:
    return RouteRequest(
        request_id="req_001",
        operation="route_request",
        state="spec_pending",
        context_digest=hashlib.sha256(b"facts").hexdigest(),
        facts=(RouteFact("entity_count", "1"),),
        allowed_routes=(Route.SPEC_EXTRACT, Route.CLARIFY, Route.ESCALATE),
    )


def _raw(route: str = "SPEC_EXTRACT", request_id: str = "req_001") -> dict[str, object]:
    return {
        "schema_version": 1,
        "request_id": request_id,
        "decision_id": "dec_001",
        "route": route,
        "reason_codes": ["adapter_test"],
        "confidence": 0.5,
    }


def test_classifier_returns_bound_typed_decision():
    request = _request()
    result = RouteClassifierAdapter(lambda _: _raw()).classify(request)
    assert result.request_id == request.request_id
    assert result.route is Route.SPEC_EXTRACT


@pytest.mark.parametrize("raw", [_raw("UNKNOWN"), {"route": "SPEC_EXTRACT"}, {"bad": True}])
def test_malformed_decisions_fail_closed(raw):
    with pytest.raises(RouteAdapterError, match="contract"):
        RouteClassifierAdapter(lambda _: raw).classify(_request())


def test_stale_and_disallowed_decisions_fail_closed():
    request = _request()
    with pytest.raises(RouteAdapterError, match="contract"):
        RouteClassifierAdapter(lambda _: _raw(request_id="other_request")).classify(request)
    with pytest.raises(RouteAdapterError, match="contract"):
        RouteClassifierAdapter(lambda _: _raw("MODIFICATION_ASSESS")).classify(request)


def test_adapter_rejects_untyped_request_and_has_no_capability_imports():
    with pytest.raises(RouteAdapterError, match="RouteRequest"):
        RouteClassifierAdapter(lambda _: _raw()).classify("not-a-request")  # type: ignore[arg-type]
    tree = ast.parse(open("src/frappe_harness/route_adapter.py", encoding="utf-8").read())
    forbidden = {"subprocess", "os", "pathlib", "requests", "frappe", "openai", "ollama", "socket"}
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert not imported & forbidden
