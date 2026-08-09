import hashlib

import pytest

from frappe_harness.monitor_adapter import MonitorAction, MonitorAdapter, MonitorAdapterError
from frappe_harness.monitor_contract import MonitorFact, build_monitor_snapshot
from frappe_harness.route_contracts import Route


def _snapshot():
    return build_monitor_snapshot(
        snapshot_id="snap_001", request_id="req_001", decision_id="dec_001",
        state="spec_pending", operation="route_request", route=Route.SPEC_EXTRACT,
        facts=(MonitorFact("entity_count", "1"),), budget_remaining=2,
        approval_present=False, lock_held=False, checkpoint_present=False,
    )


def _raw(snapshot, action="CONTINUE"):
    return {"schema_version": 1, "snapshot_id": snapshot.snapshot_id,
            "snapshot_digest": snapshot.snapshot_digest, "action": action,
            "reason_codes": ["monitor_test"]}


def test_adapter_returns_typed_bound_recommendation():
    snapshot = _snapshot()
    result = MonitorAdapter(lambda _: _raw(snapshot)).recommend(snapshot)
    assert result.action is MonitorAction.CONTINUE
    assert result.reason_codes == ("monitor_test",)


@pytest.mark.parametrize("bad", ["raw_output", "prompt", "tool"])
def test_unknown_or_unsafe_fields_fail_closed(bad):
    snapshot = _snapshot()
    raw = _raw(snapshot)
    raw[bad] = "blocked"
    with pytest.raises(MonitorAdapterError, match="unknown"):
        MonitorAdapter(lambda _: raw).recommend(snapshot)


def test_stale_snapshot_and_unknown_action_fail_closed():
    snapshot = _snapshot()
    with pytest.raises(MonitorAdapterError, match="stale"):
        MonitorAdapter(lambda _: {**_raw(snapshot), "snapshot_digest": "0" * 64}).recommend(snapshot)
    with pytest.raises(MonitorAdapterError, match="allow-listed"):
        MonitorAdapter(lambda _: _raw(snapshot, "EXECUTE")).recommend(snapshot)


def test_adapter_rejects_untyped_snapshot_and_reason_shape():
    snapshot = _snapshot()
    with pytest.raises(MonitorAdapterError, match="MonitorSnapshot"):
        MonitorAdapter(lambda _: _raw(snapshot)).recommend("bad")  # type: ignore[arg-type]
    with pytest.raises(MonitorAdapterError, match="bounded"):
        MonitorAdapter(lambda _: {**_raw(snapshot), "reason_codes": "bad"}).recommend(snapshot)
