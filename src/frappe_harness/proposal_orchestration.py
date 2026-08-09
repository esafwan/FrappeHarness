"""Bounded natural-language proposal extraction and golden equivalence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .contracts import ProjectSpec, spec_hash
from .proposal_contract import ProposalIssue, ProposalResult, parse_project_proposal


class ProposalExtractor(Protocol):
    """A data-only adapter that turns one prompt into a JSON-shaped proposal."""

    def extract(
        self, prompt: str, *, feedback: tuple[ProposalIssue, ...] = (),
    ) -> Mapping[str, Any]:
        """Return proposal data only; no execution or environment capability."""


class ProposalAdmissionError(RuntimeError):
    """A proposal is not exact-equivalent and cannot enter approval flow."""


@dataclass(frozen=True)
class ProposalExtractionAttempt:
    number: int
    result: ProposalResult


@dataclass(frozen=True)
class NaturalLanguageProposalResult:
    spec: ProjectSpec | None
    attempts: tuple[ProposalExtractionAttempt, ...]
    equivalent: bool
    escalated: bool
    reason: str | None = None


class BoundedProposalOrchestrator:
    """Allow at most two extraction/repair attempts before escalation."""

    def __init__(self, extractor: ProposalExtractor, *, max_attempts: int = 2) -> None:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1 or max_attempts > 2:
            raise ValueError("max_attempts must be 1 or 2")
        self.extractor = extractor
        self.max_attempts = max_attempts

    def extract(self, prompt: str, *, expected_spec_hash: str | None = None) -> NaturalLanguageProposalResult:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("natural-language prompt must be non-blank")
        attempts: list[ProposalExtractionAttempt] = []
        feedback: tuple[ProposalIssue, ...] = ()
        for number in range(1, self.max_attempts + 1):
            try:
                raw = self.extractor.extract(prompt, feedback=feedback)
                parsed = parse_project_proposal(raw)
            except Exception:
                parsed = ProposalResult(None, (ProposalIssue(
                    "extractor_contract_error", "proposal", "extractor returned invalid proposal data",
                ),))
            if parsed.accepted and expected_spec_hash is not None and spec_hash(parsed.require_spec()) != expected_spec_hash:
                parsed = ProposalResult(parsed.spec, (
                    ProposalIssue("golden_spec_mismatch", "proposal", "validated proposal does not match the approved golden spec hash"),
                ))
            attempts.append(ProposalExtractionAttempt(number, parsed))
            if parsed.accepted:
                # A parsed proposal is not equivalent to a golden intent unless
                # the caller supplied the immutable expected hash.  Keeping
                # this distinction explicit prevents validation-only previews
                # from being mistaken for an approval-ready equivalence pass.
                return NaturalLanguageProposalResult(
                    parsed.spec,
                    tuple(attempts),
                    expected_spec_hash is not None,
                    False,
                    None if expected_spec_hash is not None else "golden_spec_hash_not_provided",
                )
            feedback = parsed.issues
        last = attempts[-1].result if attempts else ProposalResult(None)
        return NaturalLanguageProposalResult(
            last.spec if last.accepted else None, tuple(attempts), False, True, "repair_attempts_exhausted",
        )

    def require_equivalent(self, prompt: str, *, expected_spec_hash: str) -> ProjectSpec:
        """Return a proposal only after exact golden-hash equivalence succeeds."""

        if not isinstance(expected_spec_hash, str) or len(expected_spec_hash) != 64:
            raise ProposalAdmissionError("expected specification hash must be a SHA-256 digest")
        result = self.extract(prompt, expected_spec_hash=expected_spec_hash)
        if not result.equivalent or result.spec is None:
            raise ProposalAdmissionError(result.reason or "proposal is not equivalent to the approved specification")
        return result.spec
