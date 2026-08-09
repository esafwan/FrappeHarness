"""Data-only boundary for an optional small-model proposal reviewer.

This module is intentionally a *contract*, not a model client.  A caller may
adapt a model outside the harness, but the only values it may receive and
return here are immutable proposal text, validation findings, and questions
for a human.  In particular, this boundary has no command, filesystem,
database, network, site, credential, or repository handle.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class FindingSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class ProposalItem:
    """One declared, human-readable requirement to review.

    ``key`` provides a stable reference for a later human confirmation; the
    free-form text is deliberately data, never an instruction to execute.
    """

    key: str
    text: str

    def __post_init__(self) -> None:
        if not self.key or not self.key.replace("_", "").isalnum():
            raise ValueError("proposal item key must contain only letters, digits, or underscores")
        if not self.text.strip():
            raise ValueError("proposal item text must not be blank")


@dataclass(frozen=True)
class ProposalPayload:
    """Typed input permitted to cross into a proposal-review adapter."""

    proposal_id: str
    items: tuple[ProposalItem, ...]

    def __post_init__(self) -> None:
        if not self.proposal_id or not self.proposal_id.replace("_", "").isalnum():
            raise ValueError("proposal_id must contain only letters, digits, or underscores")
        if not isinstance(self.items, tuple):
            raise ValueError("items must be an immutable tuple")
        if not self.items:
            raise ValueError("proposal must contain at least one item")
        if any(not isinstance(item, ProposalItem) for item in self.items):
            raise ValueError("items must contain ProposalItem values")
        keys = [item.key for item in self.items]
        if len(keys) != len(set(keys)):
            raise ValueError("proposal item keys must be unique")


@dataclass(frozen=True)
class ValidationFinding:
    severity: FindingSeverity
    code: str
    item_key: str
    message: str

    def __post_init__(self) -> None:
        if not self.code or not self.item_key or not self.message.strip():
            raise ValueError("finding code, item_key, and message are required")


@dataclass(frozen=True)
class Clarification:
    item_key: str
    question: str

    def __post_init__(self) -> None:
        if not self.item_key or not self.question.strip():
            raise ValueError("clarification item_key and question are required")


@dataclass(frozen=True)
class ProposalReview:
    """The complete, non-executable result of a proposal review."""

    proposal_id: str
    findings: tuple[ValidationFinding, ...] = ()
    clarifications: tuple[Clarification, ...] = ()

    def __post_init__(self) -> None:
        if not self.proposal_id:
            raise ValueError("proposal_id is required")
        if not isinstance(self.findings, tuple) or not isinstance(self.clarifications, tuple):
            raise ValueError("review collections must be immutable tuples")
        if any(not isinstance(finding, ValidationFinding) for finding in self.findings):
            raise ValueError("findings must contain ValidationFinding values")
        if any(not isinstance(question, Clarification) for question in self.clarifications):
            raise ValueError("clarifications must contain Clarification values")

    @property
    def accepted(self) -> bool:
        return not any(finding.severity is FindingSeverity.ERROR for finding in self.findings)


@runtime_checkable
class ModelGateway(Protocol):
    """An adapter may review typed data; it cannot receive capabilities."""

    def review(
        self, payload: ProposalPayload, *, feedback: tuple[ValidationFinding, ...] = (),
    ) -> ProposalReview:
        """Return findings/questions only; never execute or mutate anything."""


def validate_review(payload: ProposalPayload, review: ProposalReview) -> ProposalReview:
    """Reject an adapter result that is not bound to the submitted proposal.

    This defensive check makes review output safe to persist or present.  It
    does not call a model and intentionally has no side effects.
    """

    if review.proposal_id != payload.proposal_id:
        raise ValueError("review proposal_id does not match payload")
    valid_keys = {item.key for item in payload.items}
    unknown = {
        finding.item_key for finding in review.findings if finding.item_key not in valid_keys
    } | {question.item_key for question in review.clarifications if question.item_key not in valid_keys}
    if unknown:
        raise ValueError("review refers to unknown proposal item keys: " + ", ".join(sorted(unknown)))
    return review
