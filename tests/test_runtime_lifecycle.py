from __future__ import annotations

from pathlib import Path
import json

import pytest

from frappe_harness.compiler import compile_project
from frappe_harness.environment_preflight import (
    DiskHealth, EnvironmentIdentity, EnvironmentPreflight, FrappeRuntime, HealthCheck, SiteSafety, ToolStatus,
)
from frappe_harness.lifecycle_contract import AttemptOutcome, LifecycleState, REQUIRED_EVIDENCE
from frappe_harness.provider_plan import build_metadata_plan
from frappe_harness.proposal_orchestration import BoundedProposalOrchestrator
from frappe_harness.run_store import LockTarget, RunIntent, RunState, RunStore
from frappe_harness.runtime_lifecycle import MigrationReconciliationReceipt
from frappe_harness.runtime_lifecycle import BusinessRunReceipt, RunLifecycleCoordinator, RuntimeLifecycleError
from frappe_harness.schema_diff import classify_schema_diff
from frappe_harness.spec_io import load_project_spec


@pytest.fixture
def store(tmp_path):
    return RunStore(tmp_path / "runs.sqlite3")


def _parts():
    spec = load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")
    plan = build_metadata_plan(spec)
    return spec, plan, compile_project(spec)


def _running(store, plan):
    run = store.create_run(RunIntent(LockTarget("/bench", "dev.local", "task_tracker"), plan.provider, plan.spec_hash, plan.plan_hash))
    store.transition(run.id, RunState.PENDING_APPROVAL)
    approval = store.record_approval(run.id, approver="operator", approved=True, intent=run.intent)
    store.transition(run.id, RunState.APPROVED)
    return store.transition(run.id, RunState.RUNNING), approval


def _preflight():
    return EnvironmentPreflight(
        EnvironmentIdentity("/bench", "dev.local", "task_tracker"), FrappeRuntime(16, "16.0"),
        SiteSafety(True, False), (), (ToolStatus("bench", True), ToolStatus("frappectl", True)),
        DiskHealth(2_000_000_000, True), (HealthCheck("database", True), HealthCheck("site", True)),
    )

def _recovery(store, run):
    command = store.start_command(run.id, command="bench", arguments={"operation": "SiteBackup"})
    command = store.complete_command(command.id, exit_code=0)
    backup = store.record_lifecycle_evidence(run.id, "backup", {"recovery_receipt": "v1", "command_receipt_id": command.id, "operation": "SiteBackup", "target": {"bench": run.target.bench, "site": run.target.site, "app": run.target.app}})
    checkpoint = store.record_lifecycle_evidence(run.id, "checkpoint", {"recovery_receipt": "v1", "provider_id": "source-checkpoint", "reference": "checkpoint:1", "checkpoint": "abc", "target": {"bench": run.target.bench, "site": run.target.site, "app": run.target.app}, "spec_hash": run.intent.spec_hash, "plan_hash": run.intent.plan_hash})
    return backup, checkpoint


def test_coordinator_binds_preflight_compile_approval_and_migration_gate(store):
    spec, plan, compilation = _parts()
    run, approval = _running(store, plan)
    coordinator = RunLifecycleCoordinator(store)
    coordinator.record_preflight(run.id, _preflight())
    coordinator.record_compilation(run.id, spec, compilation)
    coordinator.record_plan_approval(run.id, plan, approval.id)
    backup, checkpoint = _recovery(store, run)
    saved = coordinator.record_migration_gate(
        run.id, plan, compilation, classify_schema_diff(spec, spec), approval.id, backup.id, checkpoint.id,
    )

    assert saved.snapshot.record_for(LifecycleState.MIGRATION_GATED).status.value == "passed"


def test_proposal_equivalence_is_typed_and_bound_into_spec_state(store):
    spec, plan, compilation = _parts()
    run, _approval = _running(store, plan)

    class Extractor:
        def extract(self, prompt, *, feedback=()):
            return json.loads(
                (Path(__file__).parents[1] / "fixtures" / "task_tracker.json").read_text()
            )

    proposal = BoundedProposalOrchestrator(Extractor()).extract(
        "Build the golden Task Tracker.", expected_spec_hash=plan.spec_hash,
    )
    coordinator = RunLifecycleCoordinator(store)
    evidence = coordinator.record_proposal_equivalence(run.id, proposal)
    assert evidence.kind == "proposal_equivalence"
    assert store.get_lifecycle_evidence_payload(evidence.id) == {
        "attempt_count": 1,
        "issue_codes": [],
        "proposal_equivalence": "v1",
        "spec_hash": plan.spec_hash,
    }

    coordinator.record_preflight(run.id, _preflight())
    snapshot = coordinator.record_compilation(
        run.id, spec, compilation, proposal_evidence_id=evidence.id,
    )
    assert snapshot.snapshot.record_for(LifecycleState.SPEC_VALIDATED).status.value == "passed"
    validated = next(
        item for item in snapshot.snapshot.record_for(LifecycleState.SPEC_VALIDATED).evidence
        if item.kind == "validated_spec"
    )
    validated_id = validated.reference.rsplit(":", 1)[-1]
    assert store.get_lifecycle_evidence_payload(validated_id)["proposal_evidence_id"] == evidence.id


def test_proposal_equivalence_rejects_preview_or_wrong_run(store):
    spec, plan, _compilation = _parts()
    run, _approval = _running(store, plan)

    class Extractor:
        def extract(self, prompt, *, feedback=()):
            return {"name": "not-a-valid-project"}

    result = BoundedProposalOrchestrator(Extractor()).extract("preview")
    with pytest.raises(RuntimeLifecycleError, match="not exact-equivalent"):
        RunLifecycleCoordinator(store).record_proposal_equivalence(run.id, result)


def test_coordinator_rejects_ineligible_preflight_and_unrecorded_or_cross_run_recovery_evidence(store):
    spec, plan, compilation = _parts()
    run, approval = _running(store, plan)
    coordinator = RunLifecycleCoordinator(store)
    bad = EnvironmentPreflight(
        EnvironmentIdentity("/bench", "dev.local", "task_tracker"), FrappeRuntime(15), SiteSafety(True, False), (), (),
        DiskHealth(0, False), (),
    )
    with pytest.raises(RuntimeLifecycleError, match="ineligible"):
        coordinator.record_preflight(run.id, bad)
    assert store.get_lifecycle_snapshot(run.id) is None

    coordinator.record_preflight(run.id, _preflight())
    coordinator.record_compilation(run.id, spec, compilation)
    coordinator.record_plan_approval(run.id, plan, approval.id)
    backup, _ = _recovery(store, run)
    with pytest.raises(Exception):
        coordinator.record_migration_gate(run.id, plan, compilation, classify_schema_diff(spec, spec), approval.id, backup.id, "missing")

    other = store.create_run(RunIntent(LockTarget("/other", "dev.local", "task_tracker"), plan.provider, plan.spec_hash, plan.plan_hash))
    store.transition(other.id, RunState.PENDING_APPROVAL)
    store.record_approval(other.id, approver="operator", approved=True, intent=other.intent)
    store.transition(other.id, RunState.APPROVED)
    other = store.transition(other.id, RunState.RUNNING)
    _, checkpoint = _recovery(store, other)
    with pytest.raises(RuntimeLifecycleError, match="another run"):
        coordinator.record_migration_gate(run.id, plan, compilation, classify_schema_diff(spec, spec), approval.id, backup.id, checkpoint.id)


def test_coordinator_requires_backend_before_frontend_and_all_verification_before_ready(store):
    spec, plan, compilation = _parts()
    run, approval = _running(store, plan)
    coordinator = RunLifecycleCoordinator(store)
    coordinator.record_preflight(run.id, _preflight())
    coordinator.record_compilation(run.id, spec, compilation)
    coordinator.record_plan_approval(run.id, plan, approval.id)
    backup, checkpoint = _recovery(store, run)
    coordinator.record_migration_gate(
        run.id, plan, compilation, classify_schema_diff(spec, spec), approval.id, backup.id, checkpoint.id,
    )
    applied = store.record_lifecycle_evidence(run.id, "migration_receipt", {"receipt": "migration"})
    store.advance_lifecycle_state(run.id, LifecycleState.MIGRATION_APPLIED, (applied.id,))
    metadata = store.record_lifecycle_evidence(run.id, "metadata_verification", {"matches": True})
    store.advance_lifecycle_state(run.id, LifecycleState.METADATA_VERIFIED, (metadata.id,))

    with pytest.raises(RuntimeLifecycleError, match="BACKEND_VERIFIED"):
        coordinator.record_frontend_verification(run.id, manifest_hash="a" * 64, scenario_count=1)

    backend = store.record_lifecycle_evidence(run.id, "backend_verification", {"passed": True})
    store.advance_lifecycle_state(run.id, LifecycleState.BACKEND_VERIFIED, (backend.id,))
    frontend = coordinator.record_frontend_verification(run.id, manifest_hash="a" * 64, scenario_count=2)
    assert frontend.snapshot.record_for(LifecycleState.FRONTEND_VERIFIED).status.value == "passed"
    ready = coordinator.record_ready(run.id, receipt={"run": run.id, "status": "ready"})
    assert ready.snapshot.record_for(LifecycleState.READY).status.value == "passed"


def test_typed_business_receipt_binds_hashes_and_persists_ready(store):
    spec, plan, compilation = _parts()
    run, approval = _running(store, plan)
    coordinator = RunLifecycleCoordinator(store)
    coordinator.record_preflight(run.id, _preflight())
    coordinator.record_compilation(run.id, spec, compilation)
    coordinator.record_plan_approval(run.id, plan, approval.id)
    backup, checkpoint = _recovery(store, run)
    coordinator.record_migration_gate(run.id, plan, compilation, classify_schema_diff(spec, spec), approval.id, backup.id, checkpoint.id)
    applied = store.record_lifecycle_evidence(run.id, "migration_receipt", {"receipt": "migration"})
    store.advance_lifecycle_state(run.id, LifecycleState.MIGRATION_APPLIED, (applied.id,))
    metadata = store.record_lifecycle_evidence(run.id, "metadata_verification", {"matches": True})
    store.advance_lifecycle_state(run.id, LifecycleState.METADATA_VERIFIED, (metadata.id,))
    backend = store.record_lifecycle_evidence(run.id, "backend_verification", {"passed": True})
    store.advance_lifecycle_state(run.id, LifecycleState.BACKEND_VERIFIED, (backend.id,))
    coordinator.record_frontend_verification(run.id, manifest_hash="a" * 64, scenario_count=2)
    receipt = BusinessRunReceipt(run.id, run.intent.spec_hash, run.intent.plan_hash, "report:82", 2, evidence_refs=(backend.id,))
    ready = coordinator.record_business_receipt(receipt)
    assert ready.snapshot.record_for(LifecycleState.READY).status.value == "passed"
    completed = coordinator.finalize_success(run.id)
    assert completed.state.value == "succeeded"
    replacement = store.create_run(run.intent)
    assert replacement.target == run.target


def test_finalize_success_requires_ready(store):
    _spec, plan, _compilation = _parts()
    run, _approval = _running(store, plan)
    coordinator = RunLifecycleCoordinator(store)
    with pytest.raises(RuntimeLifecycleError, match="requires READY"):
        coordinator.finalize_success(run.id)


def test_resume_decision_is_snapshot_bound_and_does_not_execute(store):
    _spec, plan, _compilation = _parts()
    run, _approval = _running(store, plan)
    coordinator = RunLifecycleCoordinator(store)
    coordinator.record_attempt(run.id, LifecycleState.BENCH_INSPECTED, AttemptOutcome.FAILED, failure_code="probe_failed")
    decision = coordinator.resume_decision(run.id)
    assert decision.action == "retry"
    assert decision.state is LifecycleState.BENCH_INSPECTED
    assert store.get_lifecycle_snapshot(run.id).snapshot.record_for(LifecycleState.BENCH_INSPECTED).attempts[-1].outcome is AttemptOutcome.FAILED


def test_resume_decision_requires_reconciliation_for_interrupted_migration(store):
    _spec, plan, _compilation = _parts()
    run, _approval = _running(store, plan)
    coordinator = RunLifecycleCoordinator(store)
    for state in (
        LifecycleState.BENCH_INSPECTED,
        LifecycleState.SPEC_VALIDATED,
        LifecycleState.COMPILED,
        LifecycleState.PLAN_APPROVED,
        LifecycleState.MIGRATION_GATED,
    ):
        evidence_ids = tuple(store.record_lifecycle_evidence(run.id, kind, {"test": True}).id for kind in REQUIRED_EVIDENCE[state])
        store.advance_lifecycle_state(run.id, state, evidence_ids)
    coordinator.record_attempt(run.id, LifecycleState.MIGRATION_APPLIED, AttemptOutcome.INTERRUPTED)
    decision = coordinator.resume_decision(run.id)
    assert decision.action == "manual_reconciliation"
    assert decision.state is LifecycleState.MIGRATION_APPLIED


def test_typed_business_receipt_rejects_wrong_intent_hash(store):
    _spec, plan, _compilation = _parts()
    run, _approval = _running(store, plan)
    receipt = BusinessRunReceipt(run.id, "a" * 64, run.intent.plan_hash, "report:1", 1)
    with pytest.raises(RuntimeLifecycleError, match="hashes"):
        RunLifecycleCoordinator(store).record_business_receipt(receipt)


def test_reconciliation_receipt_requires_typed_target():
    with pytest.raises(ValueError, match="LockTarget"):
        MigrationReconciliationReceipt("reconcile:1", "/bench", "a" * 64, "b" * 64)


def test_typed_migration_reconciliation_binds_exact_intent(store):
    _spec, plan, _compilation = _parts()
    run, _approval = _running(store, plan)
    store.record_lifecycle_attempt(run.id, LifecycleState.MIGRATION_APPLIED, AttemptOutcome.STARTED)
    coordinator = RunLifecycleCoordinator(store)
    receipt = MigrationReconciliationReceipt(
        "git:checkpoint-1", run.target, run.intent.spec_hash, run.intent.plan_hash,
    )
    result = coordinator.reconcile_migration(run.id, receipt)
    assert result.evidence.kind == "migration_reconciliation"
    assert result.snapshot.snapshot.record_for(LifecycleState.MIGRATION_APPLIED).status.value == "pending"
    with pytest.raises(RuntimeLifecycleError, match="run intent"):
        coordinator.reconcile_migration(
            run.id,
            MigrationReconciliationReceipt(
                "bad", LockTarget("/other", "dev.local", "task_tracker"),
                run.intent.spec_hash, run.intent.plan_hash,
            ),
        )
    with pytest.raises(RuntimeLifecycleError, match="exact target"):
        coordinator.reconcile_interrupted_migration(run.id, reconciliation={"operator": "legacy"})
