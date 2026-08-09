import pytest

from frappe_harness.monitor_adapter import MonitorAction, MonitorRecommendation
from frappe_harness.monitor_contract import MonitorFact, build_monitor_snapshot
from frappe_harness.route_contracts import Route
from frappe_harness.watchdog import WatchdogError, evaluate_watchdog


def _snapshot(**overrides):
    data = dict(snapshot_id="snap_001", request_id="req_001", decision_id="dec_001",
                state="spec_pending", operation="route_request", route=Route.SPEC_EXTRACT,
                facts=(MonitorFact("entity_count", "1"),), budget_remaining=2,
                approval_present=True, lock_held=True, checkpoint_present=True)
    data.update(overrides)
    return build_monitor_snapshot(**data)


@pytest.mark.parametrize("kwargs,reason", [
    ({"budget_remaining": 0}, "budget_exhausted"),
    ({"approval_present": False}, "approval_missing"),
    ({"lock_held": False}, "target_lock_missing"),
    ({"checkpoint_present": False}, "checkpoint_missing"),
])
def test_hard_stops_override_continue_before_mutation(kwargs, reason):
    result = evaluate_watchdog(_snapshot(**kwargs), MonitorRecommendation(MonitorAction.CONTINUE), mutation_pending=True)
    assert result.hard_stop is True
    assert result.action is MonitorAction.STOP
    assert reason in result.reason_codes


def test_invalid_digest_is_hard_stop():
    result = evaluate_watchdog(_snapshot(), MonitorRecommendation(MonitorAction.CONTINUE), digest_valid=False)
    assert result.hard_stop and result.action is MonitorAction.STOP


@pytest.mark.parametrize("action", list(MonitorAction))
def test_valid_recommendation_is_preserved_without_hard_stop(action):
    result = evaluate_watchdog(_snapshot(), MonitorRecommendation(action, ("ok",)))
    assert result.action is action
    assert result.hard_stop is False


def test_untyped_inputs_fail_closed():
    with pytest.raises(WatchdogError, match="snapshot"):
        evaluate_watchdog("bad", MonitorRecommendation(MonitorAction.STOP))  # type: ignore[arg-type]
