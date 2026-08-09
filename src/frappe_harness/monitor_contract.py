"""Immutable, redacted snapshots for the independent monitor boundary."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from .route_contracts import Route, RouteContractError


class MonitorContractError(ValueError):
    """Raised when a monitor snapshot is malformed, stale, or not redacted."""


class MonitorAction(str, Enum):
    CONTINUE = "CONTINUE"
    PAUSE = "PAUSE"
    RETRY = "RETRY"
    ESCALATE = "ESCALATE"
    STOP = "STOP"


_KEYS = frozenset(
    {
        "schema_version", "snapshot_id", "request_id", "decision_id", "state", "operation",
        "route", "facts", "budget_remaining", "approval_present", "lock_held",
        "checkpoint_present", "snapshot_digest",
    }
)
_FACT_KEYS = frozenset({"key", "value"})
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,95}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_NAMES = frozenset({"command", "file_path", "raw_output", "prompt", "shell", "tool"})
_SECRET_NAME = re.compile(r"(?i)(?:api[_-]?key|authorization|credential|password|secret|session|token)")
_SECRET = re.compile(
    r"(?i)(?:api[_-]?key|password|secret|token|authorization)\s*[:=]|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|://[^\s/:]+:[^\s@]+@"
)


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise MonitorContractError(f"{path} must be an object with string keys")
    return value


def _keys(value: Mapping[str, Any], allowed: frozenset[str], path: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise MonitorContractError(f"{path} has unknown fields: {', '.join(sorted(unknown))}")


def _id(value: object, path: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise MonitorContractError(f"{path} must be a bounded identifier")
    return value


def _name(value: object, path: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise MonitorContractError(f"{path} must be a bounded lowercase name")
    return value


@dataclass(frozen=True)
class MonitorFact:
    key: str
    value: str

    def __post_init__(self) -> None:
        key = _name(self.key, "fact.key")
        if key in _FORBIDDEN_NAMES or _SECRET_NAME.search(key):
            raise MonitorContractError("fact.key is not an allowed redacted fact name")
        if not isinstance(self.value, str) or not self.value.strip() or len(self.value) > 512:
            raise MonitorContractError("fact.value must be bounded non-blank text")
        if "\x00" in self.value or "\n" in self.value or "\r" in self.value or _SECRET.search(self.value):
            raise MonitorContractError("fact.value contains forbidden or secret-shaped content")


@dataclass(frozen=True)
class MonitorSnapshot:
    snapshot_id: str
    request_id: str
    decision_id: str
    state: str
    operation: str
    route: Route
    facts: tuple[MonitorFact, ...]
    budget_remaining: int
    approval_present: bool
    lock_held: bool
    checkpoint_present: bool
    snapshot_digest: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise MonitorContractError("unsupported monitor snapshot schema_version")
        _id(self.snapshot_id, "snapshot_id")
        _id(self.request_id, "request_id")
        _id(self.decision_id, "decision_id")
        _name(self.state, "state")
        if not isinstance(self.operation, str) or not self.operation.strip() or len(self.operation) > 64:
            raise MonitorContractError("operation must be bounded non-blank text")
        if not isinstance(self.route, Route):
            raise MonitorContractError("route must be a Route")
        if not isinstance(self.facts, tuple) or not self.facts or any(not isinstance(item, MonitorFact) for item in self.facts):
            raise MonitorContractError("facts must be a non-empty immutable tuple")
        if isinstance(self.budget_remaining, bool) or not isinstance(self.budget_remaining, int) or self.budget_remaining < 0:
            raise MonitorContractError("budget_remaining must be a non-negative integer")
        for name in ("approval_present", "lock_held", "checkpoint_present"):
            if not isinstance(getattr(self, name), bool):
                raise MonitorContractError(f"{name} must be boolean")
        if not isinstance(self.snapshot_digest, str) or not _DIGEST.fullmatch(self.snapshot_digest):
            raise MonitorContractError("snapshot_digest must be a lowercase SHA-256 digest")
        if self.snapshot_digest != snapshot_digest(self):
            raise MonitorContractError("snapshot_digest does not match snapshot contents")


def _canonical(snapshot: MonitorSnapshot) -> str:
    data = {
        "schema_version": snapshot.schema_version,
        "snapshot_id": snapshot.snapshot_id,
        "request_id": snapshot.request_id,
        "decision_id": snapshot.decision_id,
        "state": snapshot.state,
        "operation": snapshot.operation,
        "route": snapshot.route.value,
        "facts": [{"key": item.key, "value": item.value} for item in snapshot.facts],
        "budget_remaining": snapshot.budget_remaining,
        "approval_present": snapshot.approval_present,
        "lock_held": snapshot.lock_held,
        "checkpoint_present": snapshot.checkpoint_present,
    }
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def snapshot_digest(snapshot: MonitorSnapshot) -> str:
    """Return the deterministic digest without trusting the stored digest."""
    return hashlib.sha256(_canonical(snapshot).encode("utf-8")).hexdigest()


def build_monitor_snapshot(**kwargs: Any) -> MonitorSnapshot:
    """Construct a snapshot and calculate its digest from normalized fields."""
    kwargs = dict(kwargs)
    kwargs.pop("snapshot_digest", None)
    facts = kwargs.get("facts")
    if isinstance(facts, list):
        facts = tuple(MonitorFact(item["key"], item["value"]) for item in facts)
        kwargs["facts"] = facts
    provisional = object.__new__(MonitorSnapshot)
    for key, value in kwargs.items():
        object.__setattr__(provisional, key, value)
    object.__setattr__(provisional, "schema_version", kwargs.get("schema_version", 1))
    digest = snapshot_digest(provisional)
    return MonitorSnapshot(snapshot_digest=digest, **kwargs)


def parse_monitor_snapshot(raw: object) -> MonitorSnapshot:
    data = _mapping(raw, "monitor snapshot")
    _keys(data, _KEYS, "monitor snapshot")
    facts_raw = data.get("facts")
    if not isinstance(facts_raw, list) or not facts_raw or len(facts_raw) > 100:
        raise MonitorContractError("monitor snapshot.facts must be a non-empty array of at most 100")
    facts: list[MonitorFact] = []
    for index, item in enumerate(facts_raw):
        fact = _mapping(item, f"facts[{index}]")
        _keys(fact, _FACT_KEYS, f"facts[{index}]")
        facts.append(MonitorFact(fact.get("key"), fact.get("value")))
    try:
        route = Route(data.get("route"))
    except (TypeError, ValueError) as exc:
        raise MonitorContractError("route is not an allow-listed Route") from exc
    try:
        return MonitorSnapshot(
            snapshot_id=_id(data.get("snapshot_id"), "snapshot_id"),
            request_id=_id(data.get("request_id"), "request_id"),
            decision_id=_id(data.get("decision_id"), "decision_id"),
            state=_name(data.get("state"), "state"),
            operation=data.get("operation"),
            route=route,
            facts=tuple(facts),
            budget_remaining=data.get("budget_remaining"),
            approval_present=data.get("approval_present"),
            lock_held=data.get("lock_held"),
            checkpoint_present=data.get("checkpoint_present"),
            snapshot_digest=data.get("snapshot_digest"),
            schema_version=data.get("schema_version"),
        )
    except (MonitorContractError, RouteContractError):
        raise
