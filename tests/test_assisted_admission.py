import hashlib

import pytest

from frappe_harness.assisted_admission import AssistedAdmissionError, require_live_admission
from frappe_harness.monitor_adapter import MonitorAction
from frappe_harness.route_contracts import Route
from frappe_harness.route_trace import RouteTrace, trace_digest


def _trace(**overrides):
    data = dict(request_id="req_001", decision_id="dec_001", route=Route.SPEC_EXTRACT,
                policy_approved=True, worker_accepted=True,
                monitor_action=MonitorAction.CONTINUE, watchdog_action=MonitorAction.CONTINUE,
                hard_stop=False, reason_codes=(), trace_digest="0" * 64)
    data.update(overrides)
    trace = RouteTrace(**data)
    return RouteTrace(**{**trace.__dict__, "trace_digest": trace_digest(trace)})


def test_clean_trace_is_admitted():
    require_live_admission(_trace())


@pytest.mark.parametrize("overrides", [{"policy_approved": False}, {"worker_accepted": False}, {"hard_stop": True}, {"watchdog_action": MonitorAction.STOP}])
def test_any_unsafe_trace_is_rejected(overrides):
    with pytest.raises(AssistedAdmissionError):
        require_live_admission(_trace(**overrides))
