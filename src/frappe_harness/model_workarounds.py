"""Deterministic route guards for the evaluated small-model case inventory."""

from __future__ import annotations

from dataclasses import dataclass

from .route_contracts import Route


@dataclass(frozen=True)
class Workaround:
    case_id: str
    route: Route
    guard_code: str
    operator_action: str


_CASE_IDS = (
    "SUP-001", "SUP-002", "SUP-003", "SUP-004", "SUP-005",
    "AMB-001", "AMB-002", "AMB-003", "AMB-004",
    "UNS-001", "UNS-002", "UNS-003", "UNS-004",
    "NAM-001", "NAM-002", "NAM-003",
    "PER-001", "PER-002", "PER-003",
    "LNK-001", "LNK-002", "LNK-003",
    "CHG-001", "CHG-002", "DST-001", "DST-002", "DST-003", "DST-004",
)


def _route(case_id: str) -> Route:
    if case_id in {"SUP-004", "AMB-001", "AMB-002", "AMB-003", "AMB-004", "PER-001", "PER-002", "PER-003"}:
        return Route.CLARIFY
    if case_id in {"LNK-002", "CHG-001", "CHG-002"}:
        return Route.MODIFICATION_ASSESS
    if case_id.startswith("SUP-"):
        return Route.SPEC_EXTRACT
    return Route.ESCALATE


WORKAROUNDS = {
    case_id: Workaround(
        case_id, _route(case_id),
        "case_guard_" + case_id.lower().replace("-", "_"),
        "validate typed facts; require operator confirmation before progression",
    )
    for case_id in _CASE_IDS
}


def workaround_for(case_id: str) -> Workaround:
    if not isinstance(case_id, str) or case_id not in WORKAROUNDS:
        return Workaround(str(case_id), Route.ESCALATE, "unknown_case", "stop and request operator review")
    return WORKAROUNDS[case_id]
