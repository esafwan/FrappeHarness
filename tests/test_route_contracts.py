import dataclasses
import hashlib

import pytest

from frappe_harness.route_contracts import (
    Route,
    RouteContractError,
    RouteDecision,
    RouteFact,
    RouteRequest,
    parse_route_decision,
    parse_route_request,
)


def _raw_request() -> dict[str, object]:
    return {
        "schema_version": 1,
        "request_id": "req_001",
        "operation": "route_request",
        "state": "spec_pending",
        "context_digest": hashlib.sha256(b"facts").hexdigest(),
        "facts": [{"key": "entity_count", "value": "1"}],
        "allowed_routes": ["SPEC_EXTRACT", "CLARIFY", "ESCALATE"],
    }


def test_request_and_decision_parse_as_immutable_bound_typed_values():
    request = parse_route_request(_raw_request())
    decision = parse_route_decision(
        {
            "schema_version": 1,
            "request_id": "req_001",
            "decision_id": "dec_001",
            "route": "SPEC_EXTRACT",
            "reason_codes": ["complete_facts"],
            "confidence": 0.75,
        },
        request,
    )
    assert request.facts == (RouteFact("entity_count", "1"),)
    assert decision.route is Route.SPEC_EXTRACT
    assert dataclasses.is_dataclass(request) and request.__dataclass_params__.frozen
    assert dataclasses.is_dataclass(decision) and decision.__dataclass_params__.frozen


@pytest.mark.parametrize("field", ["unexpected", "tool", "command"])
def test_unknown_request_fields_are_rejected(field):
    raw = _raw_request()
    raw[field] = "do not interpret"
    with pytest.raises(RouteContractError, match="unknown fields"):
        parse_route_request(raw)


def test_unknown_decision_field_and_route_are_rejected():
    request = parse_route_request(_raw_request())
    raw = {
        "schema_version": 1,
        "request_id": "req_001",
        "decision_id": "dec_001",
        "route": "EXECUTE_SHELL",
        "reason_codes": [],
        "confidence": None,
    }
    with pytest.raises(RouteContractError, match="allow-listed"):
        parse_route_decision(raw, request)
    raw["route"] = "SPEC_EXTRACT"
    raw["extra"] = "reject"
    with pytest.raises(RouteContractError, match="unknown fields"):
        parse_route_decision(raw, request)


def test_stale_or_disallowed_decision_fails_closed():
    request = parse_route_request(_raw_request())
    stale = {
        "schema_version": 1,
        "request_id": "req_other",
        "decision_id": "dec_001",
        "route": "SPEC_EXTRACT",
        "reason_codes": [],
        "confidence": None,
    }
    with pytest.raises(RouteContractError, match="stale"):
        parse_route_decision(stale, request)
    disallowed = dict(stale, request_id="req_001", route="MODIFICATION_ASSESS")
    with pytest.raises(RouteContractError, match="allowed"):
        parse_route_decision(disallowed, request)


@pytest.mark.parametrize("key", ["api_key", "session_token", "password", "credential_id"])
def test_secret_shaped_fact_keys_are_rejected(key):
    raw = _raw_request()
    raw["facts"] = [{"key": key, "value": "redacted"}]
    with pytest.raises(RouteContractError, match="secret-shaped"):
        parse_route_request(raw)


@pytest.mark.parametrize("value", ["api_key=hidden", "https://user:secret@example.test", "-----BEGIN PRIVATE KEY-----"])
def test_secret_shaped_fact_values_and_raw_output_names_are_rejected(value):
    raw = _raw_request()
    raw["facts"] = [{"key": "provider_fact", "value": value}]
    with pytest.raises(RouteContractError, match="secret-shaped"):
        parse_route_request(raw)
    raw = _raw_request()
    raw["facts"] = [{"key": "raw_output", "value": "redacted"}]
    with pytest.raises(RouteContractError, match="allowed normalized"):
        parse_route_request(raw)


def test_parser_does_not_coerce_types_or_accept_invalid_digest():
    raw = _raw_request()
    raw["schema_version"] = "1"
    with pytest.raises(RouteContractError, match="schema_version"):
        parse_route_request(raw)
    raw = _raw_request()
    raw["context_digest"] = "not-a-digest"
    with pytest.raises(RouteContractError, match="digest"):
        parse_route_request(raw)


def test_collections_are_immutable_and_confidence_is_bounded():
    with pytest.raises(RouteContractError, match="immutable"):
        RouteRequest("req", "op", "state", "0" * 64, [RouteFact("fact", "value")], (Route.SPEC_EXTRACT,))  # type: ignore[arg-type]
    with pytest.raises(RouteContractError, match="between"):
        RouteDecision("req", "dec", Route.SPEC_EXTRACT, confidence=2.0)
