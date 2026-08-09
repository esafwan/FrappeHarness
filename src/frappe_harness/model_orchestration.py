"""Bounded repair and escalation for the data-only model review boundary."""

from __future__ import annotations

from dataclasses import dataclass

from .model_gateway import (
    Clarification,
    FindingSeverity,
    ModelGateway,
    ProposalPayload,
    ProposalReview,
    ValidationFinding,
    validate_review,
)


@dataclass(frozen=True)
class ReviewAttempt:
    number: int
    review: ProposalReview | None
    error_code: str | None = None


@dataclass(frozen=True)
class ReviewOrchestrationResult:
    attempts: tuple[ReviewAttempt, ...]
    review: ProposalReview | None
    accepted: bool
    escalated: bool
    reason: str | None = None


class BoundedReviewOrchestrator:
    """Allow at most two typed review calls, then require escalation."""

    def __init__(self, gateway: ModelGateway, *, max_attempts: int = 2) -> None:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1 or max_attempts > 2:
            raise ValueError("max_attempts must be 1 or 2")
        self.gateway = gateway
        self.max_attempts = max_attempts

    def review(self, payload: ProposalPayload) -> ReviewOrchestrationResult:
        attempts: list[ReviewAttempt] = []
        feedback: tuple[ValidationFinding, ...] = ()
        for number in range(1, self.max_attempts + 1):
            try:
                candidate = validate_review(payload, self.gateway.review(payload, feedback=feedback))
            except Exception:
                attempts.append(ReviewAttempt(number, None, "gateway_or_contract_error"))
                continue
            attempts.append(ReviewAttempt(number, candidate))
            errors = tuple(item for item in candidate.findings if item.severity is FindingSeverity.ERROR)
            if not errors and not candidate.clarifications:
                return ReviewOrchestrationResult(tuple(attempts), candidate, True, False)
            if candidate.clarifications:
                return ReviewOrchestrationResult(tuple(attempts), candidate, False, True, "clarification_required")
            feedback = errors
        last = attempts[-1].review if attempts and attempts[-1].review is not None else None
        return ReviewOrchestrationResult(tuple(attempts), last, False, True, "repair_attempts_exhausted")


def review_is_escalated(result: ReviewOrchestrationResult) -> bool:
    """Small policy helper for callers that must stop before compilation."""
    return result.escalated and not result.accepted
