"""Pure lifecycle and evidence rules for a resumable Frappe harness run.

This module is intentionally independent of :mod:`run_store`.  A durable
store may serialize these value objects, but it must not invent transitions,
evidence, or retry eligibility.  In particular, an interrupted mutation is
*not* automatically replayable: it requires an external reconciliation before
the run can proceed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Iterable, Mapping

from .operational_budget import OperationalBudget


_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class LifecycleError(ValueError):
    """Raised when a proposed lifecycle snapshot is structurally unsafe."""


class LifecycleState(str, Enum):
    """Ordered V1 states; an item may pass only after every predecessor."""

    BENCH_INSPECTED = "bench_inspected"
    SPEC_VALIDATED = "spec_validated"
    COMPILED = "compiled"
    PLAN_APPROVED = "plan_approved"
    MIGRATION_GATED = "migration_gated"
    MIGRATION_APPLIED = "migration_applied"
    METADATA_VERIFIED = "metadata_verified"
    BACKEND_VERIFIED = "backend_verified"
    FRONTEND_VERIFIED = "frontend_verified"
    READY = "ready"


ORDERED_STATES = tuple(LifecycleState)


class StateStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    INTERRUPTED = "interrupted"
    INVALIDATED = "invalidated"


class AttemptOutcome(str, Enum):
    STARTED = "started"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class Evidence:
    """An immutable, content-addressed fact attached to one lifecycle state."""

    kind: str
    digest: str
    reference: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise LifecycleError("evidence kind must be a non-blank string")
        if not isinstance(self.reference, str) or not self.reference.strip():
            raise LifecycleError("evidence reference must be a non-blank string")
        if not isinstance(self.digest, str) or not _DIGEST_RE.fullmatch(self.digest):
            raise LifecycleError("evidence digest must be a 64-character lower-case SHA-256 hex digest")


@dataclass(frozen=True)
class Attempt:
    """One immutable execution attempt.  ``number`` is one-based per state."""

    state: LifecycleState
    number: int
    outcome: AttemptOutcome
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if self.number < 1:
            raise LifecycleError("attempt number must be positive")
        if self.outcome in {AttemptOutcome.FAILED, AttemptOutcome.BLOCKED} and not self.failure_code:
            raise LifecycleError("failed or blocked attempts require a failure code")
        if self.outcome not in {AttemptOutcome.FAILED, AttemptOutcome.BLOCKED} and self.failure_code is not None:
            raise LifecycleError("only failed or blocked attempts may carry a failure code")


@dataclass(frozen=True)
class StateRecord:
    state: LifecycleState
    status: StateStatus = StateStatus.PENDING
    evidence: tuple[Evidence, ...] = ()
    attempts: tuple[Attempt, ...] = ()

    def __post_init__(self) -> None:
        if any(attempt.state != self.state for attempt in self.attempts):
            raise LifecycleError("each attempt must belong to its state record")
        numbers = tuple(attempt.number for attempt in self.attempts)
        if numbers != tuple(range(1, len(numbers) + 1)):
            raise LifecycleError("attempt numbers must be contiguous and start at one")
        if len({(item.kind, item.digest) for item in self.evidence}) != len(self.evidence):
            raise LifecycleError("duplicate evidence is not permitted")
        if self.status is StateStatus.PASSED and not self.evidence:
            raise LifecycleError("a passed state requires evidence")


@dataclass(frozen=True)
class LifecycleSnapshot:
    """Serializable state facts for exactly one immutable run intent."""

    intent_hash: str
    records: tuple[StateRecord, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.intent_hash, str) or not _DIGEST_RE.fullmatch(self.intent_hash):
            raise LifecycleError("intent hash must be a 64-character lower-case SHA-256 hex digest")
        states = tuple(record.state for record in self.records)
        if len(set(states)) != len(states):
            raise LifecycleError("a state may appear only once")
        if tuple(sorted(states, key=ORDERED_STATES.index)) != states:
            raise LifecycleError("state records must use lifecycle order")

    def record_for(self, state: LifecycleState) -> StateRecord:
        for record in self.records:
            if record.state is state:
                return record
        return StateRecord(state)


@dataclass(frozen=True)
class RetryPolicy:
    """Maximum total attempts, not additional attempts, per lifecycle state."""

    default_max_attempts: int = 3
    max_attempts: Mapping[LifecycleState, int] | None = None
    budget: OperationalBudget | None = None

    def __post_init__(self) -> None:
        if self.default_max_attempts < 1:
            raise LifecycleError("default max attempts must be positive")
        for state, maximum in (self.max_attempts or {}).items():
            if not isinstance(state, LifecycleState) or maximum < 1:
                raise LifecycleError("retry policy must map states to positive attempt limits")
        if self.budget is not None and not isinstance(self.budget, OperationalBudget):
            raise LifecycleError("retry policy budget must be an OperationalBudget")

    def limit_for(self, state: LifecycleState) -> int:
        configured = (self.max_attempts or {}).get(state, self.default_max_attempts)
        return min(configured, self.budget.max_retries) if self.budget else configured


@dataclass(frozen=True)
class TransitionDecision:
    allowed: bool
    code: str
    state: LifecycleState
    required_evidence_kinds: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResumeDecision:
    action: str
    state: LifecycleState | None
    code: str


@dataclass(frozen=True)
class InvalidationDecision:
    earliest_state: LifecycleState
    invalidated_states: tuple[LifecycleState, ...]
    retained_states: tuple[LifecycleState, ...]


# Each state is evidence-bearing.  Required kinds deliberately bind the
# mutation gate and post-mutation checks instead of letting a retry skip them.
REQUIRED_EVIDENCE: Mapping[LifecycleState, tuple[str, ...]] = {
    LifecycleState.BENCH_INSPECTED: ("environment_preflight",),
    LifecycleState.SPEC_VALIDATED: ("validated_spec",),
    LifecycleState.COMPILED: ("compilation", "artifact_audit"),
    LifecycleState.PLAN_APPROVED: ("plan", "approval"),
    LifecycleState.MIGRATION_GATED: ("migration_gate", "backup", "checkpoint"),
    LifecycleState.MIGRATION_APPLIED: ("migration_receipt",),
    LifecycleState.METADATA_VERIFIED: ("metadata_verification",),
    LifecycleState.BACKEND_VERIFIED: ("backend_verification",),
    LifecycleState.FRONTEND_VERIFIED: ("frontend_verification",),
    LifecycleState.READY: ("ready_receipt",),
}


def can_pass(snapshot: LifecycleSnapshot, state: LifecycleState, evidence: Iterable[Evidence]) -> TransitionDecision:
    """Determine whether ``state`` may pass with supplied immutable evidence."""

    evidence = tuple(evidence)
    existing = snapshot.record_for(state)
    if existing.status is StateStatus.PASSED:
        return TransitionDecision(False, "already_passed", state)
    if existing.status in {StateStatus.BLOCKED, StateStatus.INTERRUPTED}:
        return TransitionDecision(False, "state_requires_recovery", state)
    predecessor = _predecessor(state)
    if predecessor is not None and snapshot.record_for(predecessor).status is not StateStatus.PASSED:
        return TransitionDecision(False, "missing_prerequisite", state)
    expected = REQUIRED_EVIDENCE[state]
    observed = {item.kind for item in evidence}
    missing = tuple(kind for kind in expected if kind not in observed)
    if missing:
        return TransitionDecision(False, "missing_required_evidence", state, missing)
    return TransitionDecision(True, "allowed", state, expected)


def record_pass(snapshot: LifecycleSnapshot, state: LifecycleState, evidence: Iterable[Evidence]) -> LifecycleSnapshot:
    """Return a new snapshot with one legal state marked passed."""

    evidence = tuple(evidence)
    decision = can_pass(snapshot, state, evidence)
    if not decision.allowed:
        raise LifecycleError(f"cannot pass {state.value}: {decision.code}")
    current = snapshot.record_for(state)
    replacement = StateRecord(state, StateStatus.PASSED, evidence, current.attempts)
    return _replace(snapshot, replacement)


def record_attempt(snapshot: LifecycleSnapshot, state: LifecycleState, outcome: AttemptOutcome, *, failure_code: str | None = None, policy: RetryPolicy = RetryPolicy()) -> LifecycleSnapshot:
    """Record an execution result, enforcing retry limits before it is started."""

    current = snapshot.record_for(state)
    if current.status is StateStatus.PASSED:
        raise LifecycleError("a passed state cannot be retried")
    if outcome is AttemptOutcome.STARTED and len(current.attempts) >= policy.limit_for(state):
        raise LifecycleError("retry_limit_exhausted")
    if current.status is StateStatus.INTERRUPTED and state is LifecycleState.MIGRATION_APPLIED:
        raise LifecycleError("interrupted_mutation_requires_reconciliation")
    attempt = Attempt(state, len(current.attempts) + 1, outcome, failure_code)
    status = {
        AttemptOutcome.STARTED: StateStatus.RUNNING,
        AttemptOutcome.PASSED: StateStatus.PENDING,
        AttemptOutcome.FAILED: StateStatus.FAILED,
        AttemptOutcome.BLOCKED: StateStatus.BLOCKED,
        AttemptOutcome.INTERRUPTED: StateStatus.INTERRUPTED,
    }[outcome]
    return _replace(snapshot, StateRecord(state, status, current.evidence, current.attempts + (attempt,)))


def resume_decision(snapshot: LifecycleSnapshot, policy: RetryPolicy = RetryPolicy()) -> ResumeDecision:
    """Return the only safe next step after process restart or failure.

    A process that dies while a state is running is treated as interrupted.  An
    interrupted migration is never replayed automatically, because the
    provider may have applied a non-idempotent subset before the process died.
    """

    for state in ORDERED_STATES:
        record = snapshot.record_for(state)
        if record.status is StateStatus.PASSED:
            continue
        if record.status is StateStatus.RUNNING:
            if state is LifecycleState.MIGRATION_APPLIED:
                return ResumeDecision("manual_reconciliation", state, "interrupted_mutation_requires_reconciliation")
            return ResumeDecision("retry", state, "running_attempt_interrupted")
        if record.status is StateStatus.INTERRUPTED:
            if state is LifecycleState.MIGRATION_APPLIED:
                return ResumeDecision("manual_reconciliation", state, "interrupted_mutation_requires_reconciliation")
            return ResumeDecision("retry", state, "interrupted")
        if record.status is StateStatus.BLOCKED:
            return ResumeDecision("blocked", state, "state_blocked")
        if record.status is StateStatus.FAILED:
            if len(record.attempts) >= policy.limit_for(state):
                return ResumeDecision("blocked", state, "retry_limit_exhausted")
            return ResumeDecision("retry", state, "failed_retry_available")
        return ResumeDecision("start", state, "next_pending_state")
    return ResumeDecision("complete", None, "ready")


def invalidate_from(snapshot: LifecycleSnapshot, earliest_state: LifecycleState) -> InvalidationDecision:
    """Return the affected suffix for a changed input; no evidence is reused.

    Persistence may replace these records with ``INVALIDATED`` statuses, or
    create a new snapshot retaining only the returned prefix.  The function is
    pure so a caller can present the exact effect for reconfirmation first.
    """

    index = ORDERED_STATES.index(earliest_state)
    retained = ORDERED_STATES[:index]
    invalidated = ORDERED_STATES[index:]
    return InvalidationDecision(earliest_state, invalidated, retained)


def apply_invalidation(snapshot: LifecycleSnapshot, earliest_state: LifecycleState) -> LifecycleSnapshot:
    """Return a snapshot with the affected suffix explicitly invalidated."""

    decision = invalidate_from(snapshot, earliest_state)
    records: list[StateRecord] = []
    for state in decision.retained_states:
        record = snapshot.record_for(state)
        if record.status is not StateStatus.PENDING or record.evidence or record.attempts:
            records.append(record)
    for state in decision.invalidated_states:
        current = snapshot.record_for(state)
        records.append(StateRecord(state, StateStatus.INVALIDATED, (), current.attempts))
    return LifecycleSnapshot(snapshot.intent_hash, tuple(records))


def reconcile_interrupted(snapshot: LifecycleSnapshot, state: LifecycleState) -> LifecycleSnapshot:
    """Clear one interrupted state only after an explicit external reconciliation."""

    current = snapshot.record_for(state)
    if current.status not in {StateStatus.INTERRUPTED, StateStatus.RUNNING}:
        raise LifecycleError("state is not interrupted or running")
    if state is not LifecycleState.MIGRATION_APPLIED:
        raise LifecycleError("only interrupted migration requires explicit reconciliation")
    return _replace(snapshot, StateRecord(state, StateStatus.PENDING, (), current.attempts))


def _predecessor(state: LifecycleState) -> LifecycleState | None:
    index = ORDERED_STATES.index(state)
    return ORDERED_STATES[index - 1] if index else None


def _replace(snapshot: LifecycleSnapshot, replacement: StateRecord) -> LifecycleSnapshot:
    records = [record for record in snapshot.records if record.state is not replacement.state]
    records.append(replacement)
    records.sort(key=lambda record: ORDERED_STATES.index(record.state))
    return LifecycleSnapshot(snapshot.intent_hash, tuple(records))
