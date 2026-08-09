from __future__ import annotations

from frappe_harness.model_gateway import (
    Clarification, FindingSeverity, ProposalItem, ProposalPayload, ProposalReview,
    ValidationFinding,
)
from frappe_harness.model_orchestration import BoundedReviewOrchestrator, review_is_escalated


def _payload() -> ProposalPayload:
    return ProposalPayload("task_tracker", (ProposalItem("status", "Choose a task status."),))


class RepairsOnSecondCall:
    def __init__(self):
        self.feedback = []

    def review(self, payload, *, feedback=()):
        self.feedback.append(feedback)
        if not feedback:
            return ProposalReview(payload.proposal_id, (
                ValidationFinding(FindingSeverity.ERROR, "ambiguous", "status", "Choose a default."),
            ))
        return ProposalReview(payload.proposal_id)


def test_repair_is_bounded_and_feedback_is_data_only():
    gateway = RepairsOnSecondCall()
    result = BoundedReviewOrchestrator(gateway).review(_payload())
    assert result.accepted
    assert not result.escalated
    assert len(result.attempts) == 2
    assert gateway.feedback[1][0].item_key == "status"


def test_two_failed_repairs_escalate_without_a_third_call():
    class AlwaysFails:
        calls = 0
        def review(self, payload, *, feedback=()):
            self.calls += 1
            return ProposalReview(payload.proposal_id, (
                ValidationFinding(FindingSeverity.ERROR, "invalid", "status", "Still invalid."),
            ))

    gateway = AlwaysFails()
    result = BoundedReviewOrchestrator(gateway).review(_payload())
    assert review_is_escalated(result)
    assert gateway.calls == 2
    assert result.reason == "repair_attempts_exhausted"


def test_clarification_escalates_immediately():
    class NeedsHuman:
        def review(self, payload, *, feedback=()):
            return ProposalReview(payload.proposal_id, clarifications=(Clarification("status", "Which default?"),))

    result = BoundedReviewOrchestrator(NeedsHuman()).review(_payload())
    assert review_is_escalated(result)
    assert len(result.attempts) == 1
    assert result.reason == "clarification_required"
