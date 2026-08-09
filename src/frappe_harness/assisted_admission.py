"""Admission gate binding a normalized assisted trace to live mutation."""

from __future__ import annotations

from .monitor_adapter import MonitorAction
from .route_trace import RouteTrace


class AssistedAdmissionError(ValueError):
    pass


def require_live_admission(trace: RouteTrace) -> None:
    """Permit live execution only after a clean assisted trace."""
    if not isinstance(trace, RouteTrace):
        raise AssistedAdmissionError("assisted trace is required")
    if not trace.policy_approved:
        raise AssistedAdmissionError("assisted route policy was not approved")
    if trace.worker_accepted is not True:
        raise AssistedAdmissionError("assisted worker did not accept the typed task")
    if trace.hard_stop or trace.watchdog_action is not MonitorAction.CONTINUE:
        raise AssistedAdmissionError("assisted watchdog did not authorize continuation")
