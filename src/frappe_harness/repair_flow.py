"""One-attempt route repair and finite clarification boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

from .route_contracts import Route, RouteRequest


class RepairContractError(ValueError):
    pass


@dataclass(frozen=True)
class RepairOutcome:
    status: str
    accepted: bool
    attempt_count: int
    reason_codes: tuple[str, ...] = ()
    clarification_questions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in {"REPAIRED", "CLARIFY", "ESCALATED"}:
            raise RepairContractError("unknown repair status")
        if not isinstance(self.accepted, bool) or isinstance(self.attempt_count, bool) or self.attempt_count < 0:
            raise RepairContractError("invalid repair outcome fields")
        if not isinstance(self.reason_codes, tuple) or not isinstance(self.clarification_questions, tuple):
            raise RepairContractError("repair collections must be immutable tuples")


@runtime_checkable
class RepairCaller(Protocol):
    def __call__(self, request: RouteRequest, issues: tuple[str, ...]) -> Mapping[str, Any]:
        """Return only a typed repair/clarification result."""


class BoundedRepairCoordinator:
    def __init__(self, caller: RepairCaller) -> None:
        self._caller = caller

    def repair(self, request: RouteRequest, issues: tuple[str, ...]) -> RepairOutcome:
        if not isinstance(request, RouteRequest):
            raise RepairContractError("request must be a RouteRequest")
        if request.allowed_routes and Route.SPEC_REPAIR not in request.allowed_routes:
            return RepairOutcome("ESCALATED", False, 0, ("repair_route_not_allowed",))
        if not isinstance(issues, tuple) or not issues or any(not isinstance(item, str) or not item.strip() for item in issues):
            raise RepairContractError("issues must be a non-empty immutable tuple of text")
        raw = self._caller(request, issues)
        if not isinstance(raw, Mapping) or any(not isinstance(key, str) for key in raw):
            raise RepairContractError("repair result must be an object")
        unknown = set(raw) - {"accepted", "reason_codes", "clarification_questions"}
        if unknown:
            raise RepairContractError("repair result has unknown fields")
        accepted = raw.get("accepted")
        reasons = raw.get("reason_codes", [])
        questions = raw.get("clarification_questions", [])
        if not isinstance(accepted, bool) or not isinstance(reasons, list) or not isinstance(questions, list):
            raise RepairContractError("repair result has invalid fields")
        if any(not isinstance(value, str) or not value.strip() for value in reasons + questions):
            raise RepairContractError("repair result text fields must be non-blank strings")
        if questions:
            return RepairOutcome("CLARIFY", False, 1, tuple(reasons), tuple(questions))
        if accepted:
            return RepairOutcome("REPAIRED", True, 1, tuple(reasons))
        return RepairOutcome("ESCALATED", False, 1, tuple(reasons) or ("repair_rejected",))
