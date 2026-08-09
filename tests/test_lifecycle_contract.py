import pytest

from frappe_harness.lifecycle_contract import (
    AttemptOutcome,
    Evidence,
    LifecycleError,
    LifecycleSnapshot,
    LifecycleState,
    RetryPolicy,
    StateStatus,
    invalidate_from,
    record_attempt,
    record_pass,
    resume_decision,
)
from frappe_harness.operational_budget import OperationalBudget


HASH = "a" * 64


def evidence_for(state):
    from frappe_harness.lifecycle_contract import REQUIRED_EVIDENCE

    return tuple(Evidence(kind, f"{index:x}" * 64, f"test:{kind}") for index, kind in enumerate(REQUIRED_EVIDENCE[state], 1))


def passed_through(state):
    snapshot = LifecycleSnapshot(HASH)
    for candidate in tuple(LifecycleState):
        snapshot = record_pass(snapshot, candidate, evidence_for(candidate))
        if candidate is state:
            return snapshot
    raise AssertionError("unknown state")


def test_states_require_predecessors_and_all_required_evidence():
    snapshot = LifecycleSnapshot(HASH)
    with pytest.raises(LifecycleError, match="missing_prerequisite"):
        record_pass(snapshot, LifecycleState.SPEC_VALIDATED, evidence_for(LifecycleState.SPEC_VALIDATED))
    snapshot = record_pass(snapshot, LifecycleState.BENCH_INSPECTED, evidence_for(LifecycleState.BENCH_INSPECTED))
    with pytest.raises(LifecycleError, match="missing_required_evidence"):
        record_pass(snapshot, LifecycleState.SPEC_VALIDATED, ())


def test_full_lifecycle_reaches_ready_with_immutable_evidence():
    snapshot = passed_through(LifecycleState.READY)
    assert resume_decision(snapshot).action == "complete"
    assert snapshot.record_for(LifecycleState.READY).status is StateStatus.PASSED


def test_failed_state_retries_only_to_limit_then_blocks():
    snapshot = passed_through(LifecycleState.BENCH_INSPECTED)
    policy = RetryPolicy(default_max_attempts=2)
    snapshot = record_attempt(snapshot, LifecycleState.SPEC_VALIDATED, AttemptOutcome.FAILED, failure_code="invalid", policy=policy)
    assert resume_decision(snapshot, policy).action == "retry"
    snapshot = record_attempt(snapshot, LifecycleState.SPEC_VALIDATED, AttemptOutcome.FAILED, failure_code="still_invalid", policy=policy)
    assert resume_decision(snapshot, policy).code == "retry_limit_exhausted"
    with pytest.raises(LifecycleError, match="retry_limit_exhausted"):
        record_attempt(snapshot, LifecycleState.SPEC_VALIDATED, AttemptOutcome.STARTED, policy=policy)


def test_interrupted_non_mutation_is_retryable_but_migration_requires_manual_reconciliation():
    snapshot = passed_through(LifecycleState.BENCH_INSPECTED)
    snapshot = record_attempt(snapshot, LifecycleState.SPEC_VALIDATED, AttemptOutcome.INTERRUPTED)
    assert resume_decision(snapshot).action == "retry"

    snapshot = passed_through(LifecycleState.MIGRATION_GATED)
    snapshot = record_attempt(snapshot, LifecycleState.MIGRATION_APPLIED, AttemptOutcome.STARTED)
    decision = resume_decision(snapshot)
    assert decision.action == "manual_reconciliation"
    assert decision.code == "interrupted_mutation_requires_reconciliation"


def test_retry_policy_budget_caps_attempts_without_bypassing_state_rules():
    policy = RetryPolicy(default_max_attempts=5, budget=OperationalBudget(max_retries=2))
    snapshot = LifecycleSnapshot(HASH)
    snapshot = record_attempt(snapshot, LifecycleState.COMPILED, AttemptOutcome.STARTED, policy=policy)
    snapshot = record_attempt(snapshot, LifecycleState.COMPILED, AttemptOutcome.FAILED, failure_code="failed", policy=policy)
    with pytest.raises(LifecycleError, match="retry_limit_exhausted"):
        record_attempt(snapshot, LifecycleState.COMPILED, AttemptOutcome.STARTED, policy=policy)


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        (LifecycleState.BENCH_INSPECTED, tuple(LifecycleState)),
        (LifecycleState.SPEC_VALIDATED, tuple(LifecycleState)[1:]),
        (LifecycleState.MIGRATION_APPLIED, tuple(LifecycleState)[5:]),
        (LifecycleState.READY, (LifecycleState.READY,)),
    ],
)
def test_invalidation_returns_earliest_affected_suffix(changed, expected):
    decision = invalidate_from(passed_through(LifecycleState.READY), changed)
    assert decision.invalidated_states == expected
    assert set(decision.retained_states).isdisjoint(decision.invalidated_states)


def test_invalid_snapshot_and_attempt_contracts_fail_closed():
    with pytest.raises(LifecycleError, match="intent hash"):
        LifecycleSnapshot("nope")
    with pytest.raises(LifecycleError, match="failure code"):
        record_attempt(LifecycleSnapshot(HASH), LifecycleState.BENCH_INSPECTED, AttemptOutcome.FAILED)
