"""Deterministic hard-stop authority for monitor recommendations."""

from __future__ import annotations

from dataclasses import dataclass

from .monitor_adapter import MonitorAction, MonitorRecommendation
from .monitor_contract import MonitorSnapshot
from .route_contracts import Route


class WatchdogError(ValueError):
    pass


@dataclass(frozen=True)
class WatchdogOutcome:
    action: MonitorAction
    reason_codes: tuple[str, ...]
    hard_stop: bool

    def __post_init__(self) -> None:
        if not isinstance(self.action, MonitorAction):
            raise WatchdogError("action must be a MonitorAction")
        if not isinstance(self.reason_codes, tuple) or any(not isinstance(code, str) for code in self.reason_codes):
            raise WatchdogError("reason_codes must be an immutable tuple of strings")
        if not isinstance(self.hard_stop, bool):
            raise WatchdogError("hard_stop must be boolean")


def evaluate_watchdog(
    snapshot: MonitorSnapshot,
    recommendation: MonitorRecommendation,
    *,
    mutation_pending: bool = False,
    digest_valid: bool = True,
) -> WatchdogOutcome:
    """Apply Python hard stops before honoring the monitor recommendation."""
    if not isinstance(snapshot, MonitorSnapshot):
        raise WatchdogError("snapshot must be a MonitorSnapshot")
    if not isinstance(recommendation, MonitorRecommendation):
        raise WatchdogError("recommendation must be a MonitorRecommendation")
    if not isinstance(mutation_pending, bool) or not isinstance(digest_valid, bool):
        raise WatchdogError("watchdog flags must be boolean")
    reasons: list[str] = []
    if not digest_valid:
        reasons.append("snapshot_digest_invalid")
    if snapshot.budget_remaining <= 0:
        reasons.append("budget_exhausted")
    if snapshot.route not in set(Route):
        reasons.append("route_unknown")
    if mutation_pending and not snapshot.approval_present:
        reasons.append("approval_missing")
    if mutation_pending and not snapshot.lock_held:
        reasons.append("target_lock_missing")
    if mutation_pending and not snapshot.checkpoint_present:
        reasons.append("checkpoint_missing")
    if reasons:
        return WatchdogOutcome(MonitorAction.STOP, tuple(reasons), True)
    return WatchdogOutcome(recommendation.action, recommendation.reason_codes, False)
