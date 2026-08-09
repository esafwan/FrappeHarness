from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from frappe_harness.run_store import (
    ApprovalRequired,
    InvalidStateTransition,
    IntentMismatch,
    LockConflict,
    LockTarget,
    RunIntent,
    RunState,
    RunStore,
    run_intent_digest,
)
from frappe_harness.operational_budget import OperationalBudget
from frappe_harness.lifecycle_contract import AttemptOutcome, Evidence, LifecycleSnapshot, LifecycleState, REQUIRED_EVIDENCE, StateStatus, record_pass, resume_decision
from frappe_harness.spec_io import load_project_spec
from frappe_harness.test_templates import generate_verification_templates
from frappe_harness.verification_program import VerificationObservation, execute_verification_program, load_verification_program


@pytest.fixture
def store(tmp_path):
    return RunStore(tmp_path / "runs.sqlite3")


def intent(*, target=None, provider="frappe-native", spec="a", plan="b"):
    return RunIntent(target or LockTarget("bench", "site", "app"), provider, spec * 64, plan * 64)


def start_running_run(store):
    run = store.create_run(intent())
    store.transition(run.id, RunState.PENDING_APPROVAL)
    store.record_approval(run.id, approver="safwan", approved=True, intent=run.intent)
    store.transition(run.id, RunState.APPROVED)
    return store.transition(run.id, RunState.RUNNING)


def _program_and_passing_report():
    spec = load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")
    templates = generate_verification_templates(spec)
    import json
    program = load_verification_program(json.loads(templates.artifacts["harness/verification-plan.json"]))

    class Probe:
        def probe(self, request):
            return VerificationObservation(request.case.expected)

    return program, execute_verification_program(program, Probe())


def _running_program_run(store, program):
    run = store.create_run(RunIntent(LockTarget("bench", "site", "app"), "frappe-native", program.spec_hash, "b" * 64))
    store.transition(run.id, RunState.PENDING_APPROVAL)
    store.record_approval(run.id, approver="safwan", approved=True, intent=run.intent)
    store.transition(run.id, RunState.APPROVED)
    return store.transition(run.id, RunState.RUNNING)


def _passed_through_metadata(intent_hash):
    snapshot = LifecycleSnapshot(intent_hash)
    for state in tuple(LifecycleState):
        if state is LifecycleState.BACKEND_VERIFIED:
            return snapshot
        evidence = tuple(Evidence(kind, f"{index:x}" * 64, f"test:{kind}") for index, kind in enumerate(REQUIRED_EVIDENCE[state], 1))
        snapshot = record_pass(snapshot, state, evidence)
    raise AssertionError("backend state missing")


def _persist_through_metadata(store, run):
    for state in tuple(LifecycleState):
        if state is LifecycleState.BACKEND_VERIFIED:
            return
        evidence_ids = tuple(
            store.record_lifecycle_evidence(run.id, kind, {"kind": kind, "state": state.value}).id
            for kind in REQUIRED_EVIDENCE[state]
        )
        store.advance_lifecycle_state(run.id, state, evidence_ids)


def test_run_reserves_target_until_terminal_state(store):
    target = LockTarget("/tmp/bench", "demo.local", "erpnext")
    run = store.create_run(intent(target=target), metadata={"spec": "add-sales"})

    assert run.state is RunState.DRAFT
    assert store.get_run(run.id).metadata == {"spec": "add-sales"}
    with pytest.raises(LockConflict):
        store.create_run(intent(target=target))

    store.transition(run.id, RunState.PENDING_APPROVAL)
    store.record_approval(run.id, approver="safwan", approved=True, intent=run.intent)
    store.transition(run.id, RunState.APPROVED)
    store.transition(run.id, RunState.RUNNING)
    store.transition(run.id, RunState.SUCCEEDED)
    assert store.create_run(intent(target=target)).state is RunState.DRAFT


def test_run_budget_round_trips_and_is_immutable(store):
    budget = OperationalBudget(command_timeout_seconds=42.5, max_retries=5, retention_days=7)
    run = store.create_run(intent(), budget=budget)
    assert store.get_budget(run.id) == budget
    assert RunStore(store.database).get_budget(run.id) == budget
    with sqlite3.connect(store.database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE run_budgets SET budget_json = '{}' WHERE run_id = ?", (run.id,))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM run_budgets WHERE run_id = ?", (run.id,))


def test_legacy_run_without_budget_is_explicitly_unbound(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE runs (id TEXT PRIMARY KEY, bench TEXT NOT NULL, site TEXT NOT NULL, app TEXT NOT NULL,
                state TEXT NOT NULL, provider TEXT NOT NULL, spec_hash TEXT NOT NULL, plan_hash TEXT NOT NULL,
                metadata_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        """)
        connection.execute("INSERT INTO runs VALUES ('legacy', 'b', 's', 'a', 'draft', 'frappe-native', ?, ?, '{}', 't', 't')", ('a' * 64, 'b' * 64))
    store = RunStore(database)
    assert store.get_budget("legacy") is None


def test_state_machine_rejects_skipped_and_terminal_transitions(store):
    run = store.create_run(intent())
    with pytest.raises(InvalidStateTransition):
        store.transition(run.id, RunState.RUNNING)

    store.transition(run.id, RunState.CANCELLED)
    with pytest.raises(InvalidStateTransition):
        store.transition(run.id, RunState.PENDING_APPROVAL)


def test_approval_and_command_receipt_are_durable(store):
    run = start_running_run(store)
    approval = store.get_approval(store.record_approval(run.id, approver="safwan", approved=True, intent=run.intent, rationale="scope checked").id)
    receipt = store.start_command(run.id, command="install-app", arguments={"app": "erpnext"})
    completed = store.complete_command(receipt.id, exit_code=0, stdout="done")

    assert approval.decision == "approved"
    assert approval.intent == run.intent
    assert store.get_approval(approval.id).intent == run.intent
    assert completed.status == "completed"
    assert completed.exit_code == 0
    assert completed.arguments == {"app": "erpnext"}
    assert completed.stdout_bytes == len(b"done")
    assert completed.stdout_sha256 == hashlib.sha256(b"done").hexdigest()
    assert not hasattr(completed, "stdout")


def test_failed_command_receipt_retains_only_digest_and_size(store):
    run = start_running_run(store)
    receipt = store.start_command(run.id, command="migrate")
    secret_output = "migration failed: password=do-not-store"
    completed = store.complete_command(receipt.id, exit_code=1, stderr=secret_output)

    assert completed.status == "failed"
    assert completed.stderr_bytes == len(secret_output.encode("utf-8"))
    assert completed.stderr_sha256 == hashlib.sha256(secret_output.encode("utf-8")).hexdigest()
    assert not hasattr(completed, "stderr")
    with sqlite3.connect(store.database) as connection:
        persisted = " ".join(str(value) for row in connection.execute("SELECT * FROM command_receipts") for value in row)
    assert secret_output not in persisted


def test_command_receipt_requires_a_running_locked_run(store):
    run = store.create_run(intent())

    with pytest.raises(InvalidStateTransition, match="only while the run is running"):
        store.start_command(run.id, command="build")


def test_terminal_transition_waits_for_started_command_receipt(store):
    run = start_running_run(store)
    receipt = store.start_command(run.id, command="build")

    with pytest.raises(InvalidStateTransition, match="receipt is still started"):
        store.transition(run.id, RunState.SUCCEEDED)

    store.complete_command_summary(
        receipt.id,
        exit_code=None,
        stdout_bytes=0,
        stdout_sha256=hashlib.sha256(b"").hexdigest(),
        stderr_bytes=0,
        stderr_sha256=hashlib.sha256(b"").hexdigest(),
    )
    assert store.transition(run.id, RunState.SUCCEEDED).state is RunState.SUCCEEDED


@pytest.mark.parametrize("kwargs", [
    {"exit_code": True, "stdout_bytes": 0, "stdout_sha256": "a" * 64, "stderr_bytes": 0, "stderr_sha256": "b" * 64},
    {"exit_code": None, "stdout_bytes": -1, "stdout_sha256": "a" * 64, "stderr_bytes": 0, "stderr_sha256": "b" * 64},
    {"exit_code": None, "stdout_bytes": 0, "stdout_sha256": "not-a-digest", "stderr_bytes": 0, "stderr_sha256": "b" * 64},
])
def test_redacted_receipt_summary_validates_its_shape(store, kwargs):
    run = start_running_run(store)
    receipt = store.start_command(run.id, command="build")

    with pytest.raises(ValueError):
        store.complete_command_summary(receipt.id, **kwargs)


def test_initializing_a_legacy_ledger_redacts_historical_process_output(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    secret_output = "token=do-not-retain"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE command_receipts (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, command TEXT NOT NULL,
                arguments_json TEXT NOT NULL, status TEXT NOT NULL, stdout TEXT NOT NULL DEFAULT '',
                stderr TEXT NOT NULL DEFAULT '', exit_code INTEGER, created_at TEXT NOT NULL,
                completed_at TEXT
            );
            INSERT INTO command_receipts VALUES
              ('legacy', 'run', 'migrate', '{}', 'failed', '', 'token=do-not-retain', 1, 'now', 'then');
            """
        )

    RunStore(database)

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT stdout, stderr, stdout_bytes, stdout_sha256, stderr_bytes, stderr_sha256 "
            "FROM command_receipts WHERE id = 'legacy'"
        ).fetchone()
    assert row[0:2] == ("", "")
    assert row[2] == 0
    assert row[4] == len(secret_output.encode("utf-8"))
    assert row[5] == hashlib.sha256(secret_output.encode("utf-8")).hexdigest()


def test_approval_cannot_bind_a_different_provider_hash_or_target(store):
    run = store.create_run(intent())

    with pytest.raises(IntentMismatch):
        store.record_approval(
            run.id,
            approver="safwan",
            approved=True,
            intent=intent(plan="c"),
        )
    with pytest.raises(IntentMismatch):
        store.record_approval(
            run.id,
            approver="safwan",
            approved=True,
            intent=intent(target=LockTarget("other-bench", "site", "app")),
        )


def test_approved_and_running_states_require_a_persisted_matching_approval(store):
    run = store.create_run(intent())
    store.transition(run.id, RunState.PENDING_APPROVAL)

    with pytest.raises(ApprovalRequired):
        store.transition(run.id, RunState.APPROVED)

    rejection = store.record_approval(run.id, approver="safwan", approved=False, intent=run.intent, rationale="scope rejected")
    assert store.get_approval(rejection.id).decision == "rejected"
    assert store.get_run(run.id).state is RunState.PENDING_APPROVAL
    with pytest.raises(ApprovalRequired):
        store.transition(run.id, RunState.APPROVED)

    store.record_approval(run.id, approver="safwan", approved=True, intent=run.intent)
    store.transition(run.id, RunState.APPROVED)
    store.transition(run.id, RunState.RUNNING)


def test_latest_rejection_revokes_an_older_approval(store):
    run = store.create_run(intent())
    store.transition(run.id, RunState.PENDING_APPROVAL)
    store.record_approval(run.id, approver="safwan", approved=True, intent=run.intent)
    store.transition(run.id, RunState.APPROVED)
    store.record_approval(run.id, approver="safwan", approved=False, intent=run.intent, rationale="scope changed")
    with pytest.raises(ApprovalRequired):
        store.transition(run.id, RunState.RUNNING)


def test_terminal_run_cannot_receive_late_approval(store):
    run = store.create_run(intent())
    store.transition(run.id, RunState.PENDING_APPROVAL)
    store.record_approval(run.id, approver="safwan", approved=True, intent=run.intent)
    store.transition(run.id, RunState.APPROVED)
    store.transition(run.id, RunState.RUNNING)
    store.transition(run.id, RunState.SUCCEEDED)

    with pytest.raises(InvalidStateTransition, match="terminal run"):
        store.record_approval(run.id, approver="safwan", approved=False, intent=run.intent)


def test_database_rejects_turning_a_rejection_into_an_approval(store):
    run = store.create_run(intent())
    rejection = store.record_approval(run.id, approver="safwan", approved=False, intent=run.intent)

    with sqlite3.connect(store.database) as connection, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute("UPDATE approvals SET decision = 'approved' WHERE id = ?", (rejection.id,))


def test_running_rechecks_the_approval_even_if_state_is_tampered(store):
    run = store.create_run(intent())
    with sqlite3.connect(store.database) as connection:
        connection.execute("UPDATE runs SET state = 'approved' WHERE id = ?", (run.id,))

    with pytest.raises(ApprovalRequired):
        store.transition(run.id, RunState.RUNNING)


def test_run_intent_requires_pinned_sha256_hashes():
    with pytest.raises(ValueError, match="spec_hash"):
        RunIntent(LockTarget("bench", "site", "app"), "frappe-native", "not-a-hash", "b" * 64)


def test_database_rejects_intent_mutation_even_outside_the_store_api(store):
    run = store.create_run(intent())

    with sqlite3.connect(store.database) as connection, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute("UPDATE runs SET plan_hash = ? WHERE id = ?", ("c" * 64, run.id))


def test_lifecycle_snapshots_are_intent_bound_append_only_and_round_trip(store):
    run = start_running_run(store)
    intent_hash = run_intent_digest(run.intent)
    snapshot = LifecycleSnapshot(intent_hash)
    saved = store.append_lifecycle_snapshot(run.id, snapshot)

    assert saved.revision == 1
    assert store.get_lifecycle_snapshot(run.id) == saved
    with pytest.raises(InvalidStateTransition, match="advance"):
        store.append_lifecycle_snapshot(run.id, snapshot)
    with pytest.raises(IntentMismatch):
        store.append_lifecycle_snapshot(run.id, LifecycleSnapshot("a" * 64))
    with sqlite3.connect(store.database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE lifecycle_snapshots SET revision = 2 WHERE run_id = ?", (run.id,))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM lifecycle_snapshots WHERE run_id = ?", (run.id,))


def test_normalized_verification_report_is_spec_and_template_bound_without_probe_data(store):
    program, report = _program_and_passing_report()
    run = _running_program_run(store, program)
    persisted = store.record_verification_report(run.id, program, report)

    assert store.get_verification_report(persisted.id) == persisted
    with sqlite3.connect(store.database) as connection:
        stored = connection.execute("SELECT report_json FROM verification_reports WHERE id = ?", (persisted.id,)).fetchone()[0]
    assert "fixture_name" not in stored
    assert "_Test project" not in stored


def test_backend_verification_requires_a_passing_report_and_prior_lifecycle_evidence(store):
    program, report = _program_and_passing_report()
    run = _running_program_run(store, program)
    with pytest.raises(InvalidStateTransition, match="missing_prerequisite"):
        store.record_backend_verification(run.id, program, report)
    assert store.get_lifecycle_snapshot(run.id) is None

    for state in tuple(LifecycleState)[:7]:
        evidence_ids = tuple(
            store.record_lifecycle_evidence(run.id, kind, {"kind": kind, "state": state.value}).id
            for kind in REQUIRED_EVIDENCE[state]
        )
        store.advance_lifecycle_state(run.id, state, evidence_ids)
    report_record, snapshot_record = store.record_backend_verification(run.id, program, report)
    backend = snapshot_record.snapshot.record_for(LifecycleState.BACKEND_VERIFIED)

    assert backend.evidence == (Evidence("backend_verification", report_record.sha256, report_record.reference),)
    assert snapshot_record.revision == tuple(LifecycleState).index(LifecycleState.BACKEND_VERIFIED) + 1


def test_generic_snapshot_append_cannot_bypass_report_bound_backend_evidence(store):
    program, _report = _program_and_passing_report()
    run = _running_program_run(store, program)
    prior = _passed_through_metadata(run_intent_digest(run.intent))
    forged = record_pass(
        prior, LifecycleState.BACKEND_VERIFIED,
        (Evidence("backend_verification", "a" * 64, "unbound"),),
    )

    with pytest.raises(InvalidStateTransition, match="immutable evidence records"):
        store.append_lifecycle_snapshot(run.id, forged)


def test_verification_persistence_rejects_wrong_spec_or_a_failing_report(store):
    program, report = _program_and_passing_report()
    mismatched = start_running_run(store)
    with pytest.raises(IntentMismatch, match="approved spec"):
        store.record_verification_report(mismatched.id, program, report)
    store.transition(mismatched.id, RunState.FAILED)

    run = _running_program_run(store, program)
    failed = report.__class__(report.program_hash, (report.results[0].__class__(
        report.results[0].case_id, report.results[0].expected, "wrong", "failed", "unexpected_outcome",
    ),) + report.results[1:])
    with pytest.raises(InvalidStateTransition, match="failing"):
        store.record_backend_verification(run.id, program, failed)


def test_attempts_and_interrupted_migration_reconciliation_are_durable(store):
    program, _report = _program_and_passing_report()
    run = _running_program_run(store, program)
    preflight = store.record_lifecycle_evidence(run.id, "environment_preflight", {"passed": True})
    store.advance_lifecycle_state(run.id, LifecycleState.BENCH_INSPECTED, (preflight.id,))
    failed = store.record_lifecycle_attempt(run.id, LifecycleState.SPEC_VALIDATED, AttemptOutcome.FAILED, failure_code="invalid")
    assert failed.snapshot.record_for(LifecycleState.SPEC_VALIDATED).status is StateStatus.FAILED
    assert resume_decision(store.get_lifecycle_snapshot(run.id).snapshot).action == "retry"

    for state in tuple(LifecycleState)[1:5]:
        evidence_ids = tuple(
            store.record_lifecycle_evidence(run.id, kind, {"kind": kind, "state": state.value}).id
            for kind in REQUIRED_EVIDENCE[state]
        )
        store.advance_lifecycle_state(run.id, state, evidence_ids)
    started = store.record_lifecycle_attempt(run.id, LifecycleState.MIGRATION_APPLIED, AttemptOutcome.STARTED)
    assert started.snapshot.record_for(LifecycleState.MIGRATION_APPLIED).status is StateStatus.RUNNING
    interrupted = store.record_lifecycle_attempt(run.id, LifecycleState.MIGRATION_APPLIED, AttemptOutcome.INTERRUPTED)
    assert resume_decision(interrupted.snapshot).action == "manual_reconciliation"
    evidence = store.record_lifecycle_evidence(run.id, "migration_reconciliation", {"operator": "test", "result": "inspected"})
    reconciled = store.reconcile_interrupted_migration(run.id, evidence_id=evidence.id)
    assert reconciled.snapshot.record_for(LifecycleState.MIGRATION_APPLIED).status is StateStatus.PENDING


def test_spec_invalidation_persists_an_explicit_affected_suffix(store):
    program, _report = _program_and_passing_report()
    run = _running_program_run(store, program)
    _persist_through_metadata(store, run)
    evidence = store.record_lifecycle_evidence(run.id, "spec_invalidation", {
        "confirmed_spec_hash": "c" * 64, "earliest_state": LifecycleState.METADATA_VERIFIED.value,
    })
    invalidated = store.invalidate_lifecycle_from(run.id, LifecycleState.METADATA_VERIFIED, evidence_id=evidence.id)
    assert invalidated.snapshot.record_for(LifecycleState.MIGRATION_APPLIED).status is StateStatus.PASSED
    assert invalidated.snapshot.record_for(LifecycleState.METADATA_VERIFIED).status is StateStatus.INVALIDATED
    assert invalidated.snapshot.record_for(LifecycleState.BACKEND_VERIFIED).status is StateStatus.INVALIDATED
    assert invalidated.snapshot.record_for(LifecycleState.METADATA_VERIFIED).evidence == ()


def test_milestone_gate_approval_is_immutable_and_exact_intent_bound(store):
    program, _report = _program_and_passing_report()
    run = _running_program_run(store, program)
    approval = store.record_milestone_gate_approval(
        run.id, gate="VER-GATE", approver="operator", approved=True, intent=run.intent,
    )
    assert store.get_milestone_gate_approval(approval.id) == approval
    with pytest.raises(IntentMismatch):
        store.record_milestone_gate_approval(
            run.id, gate="FE-GATE", approver="operator", approved=True,
            intent=RunIntent(LockTarget("other", "site", "app"), run.intent.provider, run.intent.spec_hash, run.intent.plan_hash),
        )
    with pytest.raises(ValueError, match="unknown milestone"):
        store.record_milestone_gate_approval(run.id, gate="NOPE", approver="operator", approved=True, intent=run.intent)


def test_milestone_gate_approval_allows_succeeded_run(store):
    run = start_running_run(store)
    store.transition(run.id, RunState.SUCCEEDED)

    approval = store.record_milestone_gate_approval(
        run.id, gate="MOD-GATE", approver="operator", approved=True, intent=run.intent,
    )
    assert approval.decision == "approved"


@pytest.mark.parametrize("terminal_state", [RunState.FAILED, RunState.CANCELLED])
def test_milestone_gate_approval_rejects_failed_and_cancelled_runs(store, terminal_state):
    run = start_running_run(store)
    store.transition(run.id, terminal_state)

    with pytest.raises(InvalidStateTransition, match="terminal run"):
        store.record_milestone_gate_approval(
            run.id, gate="MOD-GATE", approver="operator", approved=True, intent=run.intent,
        )


def test_running_migration_reconciliation_is_explicit(store):
    run = start_running_run(store)
    started = store.record_lifecycle_attempt(run.id, LifecycleState.MIGRATION_APPLIED, AttemptOutcome.STARTED)
    assert started.snapshot.record_for(LifecycleState.MIGRATION_APPLIED).status is StateStatus.RUNNING
    evidence = store.record_lifecycle_evidence(run.id, "migration_reconciliation", {"operator": "test", "result": "inspected"})
    reconciled = store.reconcile_interrupted_migration(run.id, evidence_id=evidence.id)
    assert reconciled.snapshot.record_for(LifecycleState.MIGRATION_APPLIED).status is StateStatus.PENDING
