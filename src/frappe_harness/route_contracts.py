"""Strict, data-only envelopes for the routed assisted-flow boundary.

The router receives a normalized immutable fact snapshot and returns one
allow-listed route.  This module deliberately has no model, process, network,
filesystem, Frappe, or execution dependency.  It rejects unknown fields and
values instead of coercing them, so a malformed model response cannot become a
route or an authority-bearing instruction.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class RouteContractError(ValueError):
    """Raised when an envelope is malformed, stale, or outside the contract."""


class Route(str, Enum):
    SPEC_EXTRACT = "SPEC_EXTRACT"
    SPEC_REPAIR = "SPEC_REPAIR"
    MODIFICATION_ASSESS = "MODIFICATION_ASSESS"
    FACTS_EXPLAIN = "FACTS_EXPLAIN"
    TEST_DIAGNOSE = "TEST_DIAGNOSE"
    CLARIFY = "CLARIFY"
    ESCALATE = "ESCALATE"


_VERSION = 1
_MAX_ID = 128
_MAX_OPERATION = 64
_MAX_STATE = 64
_MAX_FACT_KEY = 96
_MAX_FACT_VALUE = 512
_MAX_REASON = 96
_REQUEST_KEYS = frozenset(
    {"schema_version", "request_id", "operation", "state", "context_digest", "facts", "allowed_routes"}
)
_DECISION_KEYS = frozenset(
    {"schema_version", "request_id", "decision_id", "route", "reason_codes", "confidence"}
)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,95}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SECRET_WORDS = frozenset(
    {"api_key", "apikey", "authorization", "cookie", "credential", "password", "secret", "session", "token"}
)
_FORBIDDEN_FACT_NAMES = frozenset({"command", "file_path", "raw_output", "shell", "tool", "prompt"})
_SECRET_VALUE = re.compile(
    r"(?i)(?:api[_-]?key|password|secret|token|authorization)\s*[:=]|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|://[^\s/:]+:[^\s@]+@"
)


def _require_mapping(raw: object, name: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise RouteContractError(f"{name} must be an object")
    if any(not isinstance(key, str) for key in raw):
        raise RouteContractError(f"{name} keys must be strings")
    return raw


def _strict_keys(raw: Mapping[str, Any], allowed: frozenset[str], name: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise RouteContractError(f"{name} has unknown fields: {', '.join(sorted(unknown))}")


def _version(raw: Mapping[str, Any], name: str) -> int:
    value = raw.get("schema_version")
    if isinstance(value, bool) or not isinstance(value, int) or value != _VERSION:
        raise RouteContractError(f"{name}.schema_version must be integer 1")
    return value


def _identifier(value: object, path: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise RouteContractError(f"{path} must be a bounded identifier")
    return value


def _safe_name(value: object, path: str, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit or not _SAFE_NAME.fullmatch(value):
        raise RouteContractError(f"{path} must be a bounded lowercase name")
    tokens = set(re.split(r"[_.-]", value))
    if tokens & _SECRET_WORDS or any(secret in value for secret in _SECRET_WORDS):
        raise RouteContractError(f"{path} contains a forbidden secret-shaped name")
    return value


def _text(value: object, path: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise RouteContractError(f"{path} must be non-blank text of at most {limit} characters")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise RouteContractError(f"{path} contains forbidden control characters")
    return value


@dataclass(frozen=True)
class RouteFact:
    """One normalized, non-secret fact visible to a route model."""

    key: str
    value: str

    def __post_init__(self) -> None:
        _safe_name(self.key, "fact.key", _MAX_FACT_KEY)
        if self.key in _FORBIDDEN_FACT_NAMES:
            raise RouteContractError("fact.key is not an allowed normalized fact name")
        _text(self.value, "fact.value", _MAX_FACT_VALUE)
        if _SECRET_VALUE.search(self.value):
            raise RouteContractError("fact.value contains secret-shaped content")


@dataclass(frozen=True)
class RouteRequest:
    """Python-created request sent to a route adapter."""

    request_id: str
    operation: str
    state: str
    context_digest: str
    facts: tuple[RouteFact, ...]
    allowed_routes: tuple[Route, ...]
    schema_version: int = _VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _VERSION:
            raise RouteContractError("unsupported route request schema_version")
        _identifier(self.request_id, "request_id")
        _text(self.operation, "operation", _MAX_OPERATION)
        _safe_name(self.state, "state", _MAX_STATE)
        if not isinstance(self.context_digest, str) or not _DIGEST.fullmatch(self.context_digest):
            raise RouteContractError("context_digest must be a lowercase SHA-256 digest")
        if not isinstance(self.facts, tuple) or not self.facts or any(not isinstance(item, RouteFact) for item in self.facts):
            raise RouteContractError("facts must be a non-empty immutable tuple of RouteFact")
        if not isinstance(self.allowed_routes, tuple) or not self.allowed_routes:
            raise RouteContractError("allowed_routes must be a non-empty immutable tuple")
        if any(not isinstance(route, Route) for route in self.allowed_routes):
            raise RouteContractError("allowed_routes contains an unknown route")
        if len(set(self.allowed_routes)) != len(self.allowed_routes):
            raise RouteContractError("allowed_routes must not contain duplicates")


@dataclass(frozen=True)
class RouteDecision:
    """Typed route-model output bound to exactly one request."""

    request_id: str
    decision_id: str
    route: Route
    reason_codes: tuple[str, ...] = ()
    confidence: float | None = None
    schema_version: int = _VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _VERSION:
            raise RouteContractError("unsupported route decision schema_version")
        _identifier(self.request_id, "request_id")
        _identifier(self.decision_id, "decision_id")
        if not isinstance(self.route, Route):
            raise RouteContractError("route must be an allow-listed Route")
        if not isinstance(self.reason_codes, tuple) or any(not isinstance(code, str) for code in self.reason_codes):
            raise RouteContractError("reason_codes must be an immutable tuple of strings")
        for code in self.reason_codes:
            _safe_name(code, "reason_code", _MAX_REASON)
        if self.confidence is not None:
            if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
                raise RouteContractError("confidence must be a finite number")
            if not math.isfinite(float(self.confidence)) or not 0 <= float(self.confidence) <= 1:
                raise RouteContractError("confidence must be between 0 and 1")


def parse_route_request(raw: object) -> RouteRequest:
    """Parse an already-decoded JSON-shaped request with no coercion."""

    data = _require_mapping(raw, "route request")
    _strict_keys(data, _REQUEST_KEYS, "route request")
    version = _version(data, "route request")
    facts_raw = data.get("facts")
    routes_raw = data.get("allowed_routes")
    if not isinstance(facts_raw, list) or not facts_raw or len(facts_raw) > 100:
        raise RouteContractError("route request.facts must be a non-empty array")
    if not isinstance(routes_raw, list) or not routes_raw:
        raise RouteContractError("route request.allowed_routes must be a non-empty array")
    facts: list[RouteFact] = []
    for index, item in enumerate(facts_raw):
        fact = _require_mapping(item, f"route request.facts[{index}]")
        _strict_keys(fact, frozenset({"key", "value"}), f"route request.facts[{index}]")
        facts.append(RouteFact(_safe_name(fact.get("key"), f"facts[{index}].key", _MAX_FACT_KEY), _text(fact.get("value"), f"facts[{index}].value", _MAX_FACT_VALUE)))
    routes: list[Route] = []
    for index, value in enumerate(routes_raw):
        try:
            routes.append(Route(value))
        except (TypeError, ValueError) as exc:
            raise RouteContractError(f"allowed_routes[{index}] is not an allow-listed route") from exc
    return RouteRequest(
        _identifier(data.get("request_id"), "request_id"),
        _text(data.get("operation"), "operation", _MAX_OPERATION),
        _safe_name(data.get("state"), "state", _MAX_STATE),
        data.get("context_digest"),
        tuple(facts),
        tuple(routes),
        version,
    )


def parse_route_decision(raw: object, request: RouteRequest) -> RouteDecision:
    """Parse and bind one decision; mismatched/stale requests fail closed."""

    if not isinstance(request, RouteRequest):
        raise RouteContractError("request must be a RouteRequest")
    data = _require_mapping(raw, "route decision")
    _strict_keys(data, _DECISION_KEYS, "route decision")
    version = _version(data, "route decision")
    request_id = _identifier(data.get("request_id"), "decision.request_id")
    if request_id != request.request_id:
        raise RouteContractError("route decision is stale or bound to another request")
    try:
        route = Route(data.get("route"))
    except (TypeError, ValueError) as exc:
        raise RouteContractError("decision.route is not an allow-listed route") from exc
    if route not in request.allowed_routes:
        raise RouteContractError("decision.route is not allowed for this request")
    reason_raw = data.get("reason_codes", [])
    if not isinstance(reason_raw, list) or len(reason_raw) > 10:
        raise RouteContractError("decision.reason_codes must be an array")
    confidence = data.get("confidence")
    return RouteDecision(
        request_id,
        _identifier(data.get("decision_id"), "decision_id"),
        route,
        tuple(_safe_name(code, "reason_code", _MAX_REASON) for code in reason_raw),
        confidence,
        version,
    )
