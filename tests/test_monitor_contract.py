import hashlib

import pytest

from frappe_harness.monitor_contract import (
    MonitorContractError,
    MonitorFact,
    MonitorSnapshot,
    build_monitor_snapshot,
    parse_monitor_snapshot,
    snapshot_digest,
)
from frappe_harness.route_contracts import Route


def _kwargs():
    return {
        "schema_version": 1,
        "snapshot_id": "snap_001",
        "request_id": "req_001",
        "decision_id": "dec_001",
        "state": "spec_pending",
        "operation": "route_request",
        "route": Route.SPEC_EXTRACT,
        "facts": (MonitorFact("entity_count", "1"),),
        "budget_remaining": 2,
        "approval_present": False,
        "lock_held": False,
        "checkpoint_present": False,
    }


def test_build_and_parse_snapshot_binds_deterministic_digest():
    snapshot = build_monitor_snapshot(**_kwargs())
    assert snapshot.snapshot_digest == snapshot_digest(snapshot)
    raw = {**_kwargs(), "route": "SPEC_EXTRACT", "facts": [{"key": "entity_count", "value": "1"}], "schema_version": 1, "snapshot_digest": snapshot.snapshot_digest}
    assert parse_monitor_snapshot(raw) == snapshot


def test_digest_is_stable_for_same_normalized_contents():
    first = build_monitor_snapshot(**_kwargs())
    second = build_monitor_snapshot(**_kwargs())
    assert first.snapshot_digest == second.snapshot_digest


def test_unknown_fields_and_digest_drift_fail_closed():
    snapshot = build_monitor_snapshot(**_kwargs())
    raw = {**_kwargs(), "snapshot_digest": snapshot.snapshot_digest, "raw_output": "never"}
    with pytest.raises(MonitorContractError, match="unknown fields"):
        parse_monitor_snapshot(raw)
    raw = {**_kwargs(), "facts": [{"key": "entity_count", "value": "1"}], "snapshot_digest": "0" * 64}
    with pytest.raises(MonitorContractError, match="does not match"):
        parse_monitor_snapshot(raw)


@pytest.mark.parametrize("value", ["api_key=hidden", "https://user:secret@example.test", "-----BEGIN PRIVATE KEY-----"])
def test_secret_shaped_fact_values_are_rejected(value):
    raw = {**_kwargs(), "snapshot_digest": "0" * 64, "facts": [{"key": "provider_fact", "value": value}]}
    with pytest.raises(MonitorContractError, match="forbidden|secret-shaped"):
        parse_monitor_snapshot(raw)


def test_secret_shaped_fact_names_are_rejected():
    raw = {**_kwargs(), "facts": [{"key": "session_token", "value": "redacted"}], "snapshot_digest": "0" * 64}
    with pytest.raises(MonitorContractError, match="forbidden|redacted"):
        parse_monitor_snapshot(raw)


def test_invalid_types_and_immutable_values_fail_closed():
    with pytest.raises(MonitorContractError, match="immutable"):
        MonitorSnapshot(snapshot_digest="0" * 64, **{**_kwargs(), "facts": [MonitorFact("fact", "value")]})  # type: ignore[arg-type]
    raw = {**_kwargs(), "facts": [{"key": "entity_count", "value": "1"}], "snapshot_digest": "0" * 64, "budget_remaining": True}
    with pytest.raises(MonitorContractError, match="non-negative"):
        parse_monitor_snapshot(raw)
