import pytest

from frappe_harness.milestone_gates import MilestoneGateInput, evaluate_all_milestone_gates, evaluate_milestone_gate, evaluate_milestone_gate_for_run
from frappe_harness.run_store import InvalidStateTransition, LockTarget, RunIntent, RunState, RunStore


def test_evidence_alone_never_passes_a_milestone_gate():
    facts = MilestoneGateInput({"compiler_golden": True}, {})
    result = evaluate_milestone_gate("CMP-GATE", facts)
    assert not result.passed
    assert "explicit_approval_required" in result.failures


def test_release_gate_requires_approval_evidence_thresholds_and_zero_unsafe_admissions():
    facts = MilestoneGateInput(
        {"release_pack": True}, {"REL-GATE": True}, model_thresholds_passed=True,
    )
    result = evaluate_milestone_gate("REL-GATE", facts)
    assert not result.passed
    assert result.failures == ("corpus_coverage_must_be_complete", "unsafe_admissions_must_be_zero", "uncontrolled_command_paths_must_be_zero")

    passed = evaluate_milestone_gate(
        "REL-GATE", MilestoneGateInput(
            {"release_pack": True}, {"REL-GATE": True}, model_thresholds_passed=True,
            corpus_coverage_complete=True, unsafe_admissions_zero=True,
            uncontrolled_command_paths_zero=True,
        ),
    )
    assert passed.passed


def test_all_gate_decisions_are_explicit_and_deterministic():
    results = evaluate_all_milestone_gates(MilestoneGateInput({}, {}))
    assert tuple(item.gate for item in results) == (
        "CMP-GATE", "BEX-GATE", "VAL-GATE", "VER-GATE", "FE-GATE",
        "ORC-GATE", "LLM-GATE", "PRV-GATE", "PI-GATE", "MOD-GATE", "REL-GATE",
    )
    assert not any(item.passed for item in results)


def test_run_backed_gate_helper_requires_durable_exact_approval(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    intent = RunIntent(LockTarget("/bench", "dev.local", "task_tracker"), "frappe-native", "a" * 64, "b" * 64)
    run = store.create_run(intent)
    store.transition(run.id, RunState.PENDING_APPROVAL)
    store.record_approval(run.id, approver="operator", approved=True, intent=intent)
    store.transition(run.id, RunState.APPROVED)
    store.transition(run.id, RunState.RUNNING)
    facts = {"compiler_golden": True}
    pending = evaluate_milestone_gate_for_run(store, run.id, "CMP-GATE", evidence=facts)
    assert not pending.passed
    store.record_milestone_gate_approval(run.id, gate="CMP-GATE", approver="operator", approved=True, intent=intent)
    evidence = store.record_lifecycle_evidence(run.id, "compiler_golden", {"passed": True})
    passed = evaluate_milestone_gate_for_run(store, run.id, "CMP-GATE", evidence_refs={"compiler_golden": evidence.reference})
    assert passed.passed


@pytest.mark.parametrize("state", [RunState.DRAFT, RunState.PENDING_APPROVAL])
def test_gate_approval_rejects_before_operational_approval(tmp_path, state):
    store = RunStore(tmp_path / f"runs-{state.value}.sqlite3")
    intent = RunIntent(LockTarget("/bench", "dev.local", "task_tracker"), "frappe-native", "a" * 64, "b" * 64)
    run = store.create_run(intent)
    if state is RunState.PENDING_APPROVAL:
        store.transition(run.id, RunState.PENDING_APPROVAL)
    with pytest.raises(InvalidStateTransition, match="before operational approval"):
        store.record_milestone_gate_approval(
            run.id, gate="CMP-GATE", approver="operator", approved=True, intent=intent
        )


def test_gate_approval_allows_succeeded_terminal_run(tmp_path):
    store = RunStore(tmp_path / "runs-succeeded.sqlite3")
    intent = RunIntent(LockTarget("/bench", "dev.local", "task_tracker"), "frappe-native", "a" * 64, "b" * 64)
    run = store.create_run(intent)
    store.transition(run.id, RunState.PENDING_APPROVAL)
    store.record_approval(run.id, approver="operator", approved=True, intent=intent)
    store.transition(run.id, RunState.APPROVED)
    store.transition(run.id, RunState.RUNNING)
    evidence = store.record_lifecycle_evidence(run.id, "compiler_golden", {"passed": True})
    store.transition(run.id, RunState.SUCCEEDED)
    approval = store.record_milestone_gate_approval(
        run.id, gate="CMP-GATE", approver="operator", approved=True, intent=intent
    )
    assert approval.decision == "approved"
    passed = evaluate_milestone_gate_for_run(
        store, run.id, "CMP-GATE", evidence_refs={"compiler_golden": evidence.reference}
    )
    assert passed.passed


@pytest.mark.parametrize("state", [RunState.FAILED, RunState.CANCELLED])
def test_gate_approval_rejects_failed_and_cancelled_terminal_runs(tmp_path, state):
    store = RunStore(tmp_path / f"runs-{state.value}.sqlite3")
    intent = RunIntent(LockTarget("/bench", "dev.local", "task_tracker"), "frappe-native", "a" * 64, "b" * 64)
    run = store.create_run(intent)
    store.transition(run.id, RunState.PENDING_APPROVAL)
    store.record_approval(run.id, approver="operator", approved=True, intent=intent)
    store.transition(run.id, RunState.APPROVED)
    store.transition(run.id, RunState.RUNNING)
    store.transition(run.id, state)
    with pytest.raises(InvalidStateTransition, match="terminal run"):
        store.record_milestone_gate_approval(
            run.id, gate="FE-GATE", approver="operator", approved=True, intent=intent
        )


def test_run_backed_gate_rejects_cross_run_evidence_and_later_rejection(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    intent = RunIntent(LockTarget("/bench", "dev.local", "task_tracker"), "frappe-native", "a" * 64, "b" * 64)
    run = store.create_run(intent)
    store.transition(run.id, RunState.PENDING_APPROVAL)
    store.record_approval(run.id, approver="operator", approved=True, intent=intent)
    store.transition(run.id, RunState.APPROVED)
    store.transition(run.id, RunState.RUNNING)
    evidence = store.record_lifecycle_evidence(run.id, "compiler_golden", {"passed": True})
    store.record_milestone_gate_approval(run.id, gate="CMP-GATE", approver="operator", approved=True, intent=intent)
    store.record_milestone_gate_approval(run.id, gate="CMP-GATE", approver="operator", approved=False, intent=intent)
    decision = evaluate_milestone_gate_for_run(store, run.id, "CMP-GATE", evidence_refs={"compiler_golden": evidence.reference})
    assert not decision.passed
    assert "explicit_approval_required" in decision.failures
    other = store.create_run(RunIntent(LockTarget("/other", "dev.local", "task_tracker"), "frappe-native", "a" * 64, "b" * 64))
    store.transition(other.id, RunState.PENDING_APPROVAL)
    store.record_approval(other.id, approver="operator", approved=True, intent=other.intent)
    store.transition(other.id, RunState.APPROVED)
    store.transition(other.id, RunState.RUNNING)
    other_evidence = store.record_lifecycle_evidence(other.id, "compiler_golden", {"passed": True})
    with pytest.raises(ValueError, match="not bound"):
        evaluate_milestone_gate_for_run(store, run.id, "CMP-GATE", evidence_refs={"compiler_golden": other_evidence.reference})


def test_run_backed_release_gate_never_trusts_scalar_diagnostics(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    intent = RunIntent(LockTarget("/bench", "dev.local", "task_tracker"), "frappe-native", "a" * 64, "b" * 64)
    run = store.create_run(intent)
    store.transition(run.id, RunState.PENDING_APPROVAL)
    store.record_approval(run.id, approver="operator", approved=True, intent=intent)
    store.transition(run.id, RunState.APPROVED)
    store.transition(run.id, RunState.RUNNING)
    store.record_milestone_gate_approval(run.id, gate="REL-GATE", approver="operator", approved=True, intent=intent)
    evidence = store.record_lifecycle_evidence(run.id, "release_pack", {"passed": True})
    decision = evaluate_milestone_gate_for_run(
        store,
        run.id,
        "REL-GATE",
        evidence_refs={"release_pack": evidence.reference},
        model_thresholds_passed=True,
        corpus_coverage_complete=True,
        unsafe_admissions_zero=True,
        uncontrolled_command_paths_zero=True,
    )
    assert not decision.passed
    assert "model_thresholds_required" in decision.failures


def test_run_backed_gate_rejects_mismatched_evidence_kind(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    intent = RunIntent(LockTarget("/bench", "dev.local", "task_tracker"), "frappe-native", "a" * 64, "b" * 64)
    run = store.create_run(intent)
    store.transition(run.id, RunState.PENDING_APPROVAL)
    store.record_approval(run.id, approver="operator", approved=True, intent=intent)
    store.transition(run.id, RunState.APPROVED)
    store.transition(run.id, RunState.RUNNING)
    store.record_milestone_gate_approval(run.id, gate="CMP-GATE", approver="operator", approved=True, intent=intent)
    unrelated = store.record_lifecycle_evidence(run.id, "safety_validation", {"passed": True})
    with pytest.raises(ValueError, match="unrelated kind"):
        evaluate_milestone_gate_for_run(store, run.id, "CMP-GATE", evidence_refs={"compiler_golden": unrelated.reference})
