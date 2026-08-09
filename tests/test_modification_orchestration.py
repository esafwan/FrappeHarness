from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3
import pytest

from frappe_harness.contracts import FieldType, spec_hash
from frappe_harness.lifecycle_contract import LifecycleState
from frappe_harness.modification_orchestration import (
    AffectedCheckEvidence,
    RunBoundAffectedCheckExecutor,
    RunBoundModificationCoordinator,
    RunBoundSuccessorModificationExecutor,
    assess_modification,
    compose_modification_gate_report,
)
from frappe_harness.milestone_gates import evaluate_milestone_gate_for_run
from frappe_harness.provider_plan import build_metadata_plan
from frappe_harness.run_store import IntentMismatch, LockTarget, RunIntent, RunState, RunStore
from frappe_harness.schema_diff import classify_schema_diff
from frappe_harness.operational_budget import OperationalBudget
from frappe_harness.spec_io import load_project_spec
from test_migration_execution import FakeProvider, _prepared_store


def _specs():
    root = Path(__file__).parents[1]
    return load_project_spec(root / "fixtures" / "task_tracker.json"), load_project_spec(root / "fixtures" / "task_tracker_v2_reference_url.json")


def test_safe_v2_modification_requires_exact_reconfirmation_and_invalidates_from_gate():
    installed, confirmed = _specs()
    pending = assess_modification(installed, confirmed)
    assert not pending.allowed
    assert pending.reason == "reconfirmation_required"
    assert pending.invalidation.earliest_state is LifecycleState.MIGRATION_GATED

    approved = assess_modification(installed, confirmed, reconfirmed_spec_hash=spec_hash(confirmed))
    assert approved.allowed
    assert approved.reconfirmed
    assert approved.diff.safe


def test_blocked_destructive_change_cannot_be_approved_by_reconfirmation():
    installed, confirmed = _specs()
    entity = confirmed.entities[1]
    changed_fields = tuple(
        replace(field, field_type=FieldType.DATA, options=()) if field.name == "priority" else field
        for field in entity.fields
    )
    changed_entity = replace(entity, fields=changed_fields)
    removed = replace(confirmed, entities=tuple(
        changed_entity if item.name == entity.name else item for item in confirmed.entities
    ))
    assessment = assess_modification(installed, removed, reconfirmed_spec_hash=spec_hash(removed))
    assert not assessment.allowed
    assert assessment.reason == "blocked_schema_diff"
    assert assessment.diff.blocked


def test_blocked_destructive_assessment_records_no_mutation_evidence(tmp_path):
    installed, confirmed = _specs()
    _spec, plan, _compilation, store, run, _approval, _backup, _checkpoint = _prepared_store(tmp_path)
    entity = confirmed.entities[1]
    changed_fields = tuple(
        replace(field, field_type=FieldType.DATA, options=()) if field.name == "priority" else field
        for field in entity.fields
    )
    destructive = replace(confirmed, entities=tuple(
        replace(entity, fields=changed_fields) if item.name == entity.name else item for item in confirmed.entities
    ))
    coordinator = RunBoundModificationCoordinator(store)
    assessment = assess_modification(installed, destructive, reconfirmed_spec_hash=spec_hash(destructive))
    evidence = coordinator.record_destructive_no_mutation(run.id, assessment)
    payload = store.get_lifecycle_evidence_payload(evidence.id)

    assert evidence.kind == "destructive_no_mutation"
    assert payload["mutation_attempted"] is False
    assert "field_type" in payload["blocked_change_codes"]


def test_run_bound_affected_checks_persist_four_exact_evidence_refs(tmp_path):
    _spec, plan, _compilation, store, run, _approval, _backup, _checkpoint = _prepared_store(tmp_path)

    class Provider:
        def execute(self, run_id, check):
            assert run_id == run.id
            result = {"passed": True, "target": {"bench": run.target.bench, "site": run.target.site, "app": run.target.app}, "spec_hash": run.intent.spec_hash, "plan_hash": run.intent.plan_hash}
            if check == "destructive_no_mutation":
                result["mutation_attempted"] = False
            return result

    evidence = RunBoundAffectedCheckExecutor(store, Provider()).execute(run.id)
    refs = (evidence.migration_ref, evidence.preservation_ref, evidence.browser_ref, evidence.destructive_no_mutation_ref)
    assert all(ref.startswith(f"run:{run.id}:evidence:") for ref in refs)
    assert store.get_lifecycle_evidence_payload(evidence.destructive_no_mutation_ref.rsplit(":", 1)[-1])["mutation_attempted"] is False


def test_run_bound_affected_checks_persist_browser_attestation(tmp_path):
    _spec, plan, _compilation, store, run, _approval, _backup, _checkpoint = _prepared_store(tmp_path)

    class Provider:
        def execute(self, run_id, check):
            result = {
                "passed": True,
                "target": {"bench": run.target.bench, "site": run.target.site, "app": run.target.app},
                "spec_hash": run.intent.spec_hash,
                "plan_hash": run.intent.plan_hash,
            }
            if check == "browser":
                result["browser_attestation"] = {"url": "https://site.local/task_tracker.html", "passed": True}
            if check == "destructive_no_mutation":
                result["mutation_attempted"] = False
            return result

    evidence = RunBoundAffectedCheckExecutor(store, Provider()).execute(run.id)
    payload = store.get_lifecycle_evidence_payload(evidence.browser_ref.rsplit(":", 1)[-1])
    assert payload["browser_attestation"]["url"].startswith("https://")


def test_run_bound_affected_checks_can_bind_reconfirmed_v2_hashes(tmp_path):
    _spec, plan, _compilation, store, run, _approval, _backup, _checkpoint = _prepared_store(tmp_path)
    v2_spec = load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker_v2_reference_url.json")
    v2_plan = build_metadata_plan(v2_spec, provider="frappe-native")

    class Provider:
        def execute(self, run_id, check):
            result = {
                "passed": True,
                "target": {"bench": run.target.bench, "site": run.target.site, "app": run.target.app},
                "spec_hash": spec_hash(v2_spec),
                "plan_hash": v2_plan.plan_hash,
            }
            return result | ({"mutation_attempted": False} if check == "destructive_no_mutation" else {})

    evidence = RunBoundAffectedCheckExecutor(store, Provider()).execute(
        run.id, expected_spec_hash=spec_hash(v2_spec), expected_plan_hash=v2_plan.plan_hash,
    )
    payload = store.get_lifecycle_evidence_payload(evidence.migration_ref.rsplit(":", 1)[-1])
    assert payload["spec_hash"] == spec_hash(v2_spec)
    assert payload["plan_hash"] == v2_plan.plan_hash


def test_run_bound_affected_checks_reject_failed_destructive_attestation(tmp_path):
    _spec, _plan, _compilation, store, run, _approval, _backup, _checkpoint = _prepared_store(tmp_path)

    class Provider:
        def execute(self, run_id, check):
            result = {"passed": True, "target": {"bench": run.target.bench, "site": run.target.site, "app": run.target.app}, "spec_hash": run.intent.spec_hash, "plan_hash": run.intent.plan_hash}
            return result | ({"mutation_attempted": True} if check == "destructive_no_mutation" else {})

    with pytest.raises(ValueError, match="mutation_attempted=false"):
        RunBoundAffectedCheckExecutor(store, Provider()).execute(run.id)


def test_run_bound_affected_checks_reject_mutation_attestation_on_non_destructive_check(tmp_path):
    _spec, _plan, _compilation, store, run, _approval, _backup, _checkpoint = _prepared_store(tmp_path)

    class Provider:
        def execute(self, run_id, check):
            result = {
                "passed": True,
                "target": {"bench": run.target.bench, "site": run.target.site, "app": run.target.app},
                "spec_hash": run.intent.spec_hash,
                "plan_hash": run.intent.plan_hash,
            }
            if check == "migration":
                result["mutation_attempted"] = True
            elif check == "destructive_no_mutation":
                result["mutation_attempted"] = False
            return result

    with pytest.raises(ValueError, match="migration.*mutation_attempted=false"):
        RunBoundAffectedCheckExecutor(store, Provider()).execute(run.id)


def test_run_bound_affected_checks_do_not_persist_partial_evidence(tmp_path):
    _spec, _plan, _compilation, store, run, _approval, _backup, _checkpoint = _prepared_store(tmp_path)

    class Provider:
        def execute(self, run_id, check):
            result = {"passed": check != "browser", "target": {"bench": run.target.bench, "site": run.target.site, "app": run.target.app}, "spec_hash": run.intent.spec_hash, "plan_hash": run.intent.plan_hash}
            return result

    with pytest.raises(ValueError, match="browser"):
        RunBoundAffectedCheckExecutor(store, Provider()).execute(run.id)
    with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
        count = conn.execute("SELECT COUNT(*) FROM lifecycle_evidence WHERE run_id = ? AND kind LIKE 'modification_%'", (run.id,)).fetchone()[0]
    assert count == 0


def test_run_bound_affected_checks_reject_target_drift(tmp_path):
    _spec, _plan, _compilation, store, run, _approval, _backup, _checkpoint = _prepared_store(tmp_path)

    class Provider:
        def execute(self, run_id, check):
            return {"passed": True, "target": {"bench": "/other", "site": run.target.site, "app": run.target.app}, "spec_hash": run.intent.spec_hash, "plan_hash": run.intent.plan_hash}

    with pytest.raises(ValueError, match="not bound to this run intent"):
        RunBoundAffectedCheckExecutor(store, Provider()).execute(run.id)


def test_run_bound_safe_modification_persists_assessment_and_invalidates_suffix(tmp_path):
    installed, confirmed = _specs()
    _spec, plan, compilation, store, run, approval, backup, checkpoint = _prepared_store(tmp_path)
    from frappe_harness.migration_execution import RunBoundMigrationExecutor
    from frappe_harness.schema_diff import classify_schema_diff

    RunBoundMigrationExecutor(store).execute(
        run.id, plan=plan, compilation=compilation, schema_diff=classify_schema_diff(installed, installed),
        approval_id=approval.id, backup_evidence_id=backup.id, checkpoint_evidence_id=checkpoint.id,
        provider=FakeProvider(),
    )
    result = RunBoundModificationCoordinator(store).assess(
        run.id, installed, confirmed, reconfirmed_spec_hash=spec_hash(confirmed),
    )
    assert result.assessment.allowed
    assert result.invalidated_snapshot is not None
    assert result.invalidated_snapshot.snapshot.record_for(LifecycleState.MIGRATION_GATED).status.value == "invalidated"
    assert store.get_lifecycle_evidence_payload(result.evidence_id)["allowed"] is True
    v2_plan = build_metadata_plan(confirmed, provider=plan.provider)
    successor = RunBoundModificationCoordinator(store).create_revision_run(run.id, result, plan=v2_plan)
    assert successor.intent.spec_hash == spec_hash(confirmed)
    assert successor.intent.plan_hash == v2_plan.plan_hash
    assert successor.metadata["revision_of"] == run.id
    assert store.get_run(run.id).state is RunState.CANCELLED
    store.transition(successor.id, RunState.PENDING_APPROVAL)
    with pytest.raises(IntentMismatch):
        store.record_approval(successor.id, approver="operator", approved=True, intent=run.intent)
    store.record_approval(successor.id, approver="operator", approved=True, intent=successor.intent)
    store.transition(successor.id, RunState.APPROVED)


def test_v2_gate_report_stays_pending_without_explicit_approval():
    installed, confirmed = _specs()
    assessment = assess_modification(installed, confirmed, reconfirmed_spec_hash=spec_hash(confirmed))
    report = compose_modification_gate_report(
        assessment,
        migration_ref="run:v2:migration",
        preservation_ref="run:v2:preservation",
        browser_ref="run:v2:browser",
        destructive_no_mutation_ref="run:v2:destructive",
    )
    assert report.status == "pending"
    approved = compose_modification_gate_report(
        assessment,
        migration_ref="run:v2:migration",
        preservation_ref="run:v2:preservation",
        browser_ref="run:v2:browser",
        destructive_no_mutation_ref="run:v2:destructive",
        approval_ref="approval:m8",
    )
    assert approved.status == "approved"


def test_v2_gate_approval_must_be_durable_exact_m8_evidence(tmp_path):
    installed, confirmed = _specs()
    assessment = assess_modification(installed, confirmed, reconfirmed_spec_hash=spec_hash(confirmed))
    store = RunStore(tmp_path / "runs.sqlite3")
    plan = build_metadata_plan(installed)
    run = store.create_run(RunIntent(LockTarget("/bench", "dev.local", "task_tracker"), plan.provider, plan.spec_hash, plan.plan_hash))
    store.transition(run.id, RunState.PENDING_APPROVAL)
    store.record_approval(run.id, approver="operator", approved=True, intent=run.intent)
    store.transition(run.id, RunState.APPROVED)
    store.transition(run.id, RunState.RUNNING)
    refs = tuple(store.record_lifecycle_evidence(run.id, kind, {"proof": kind}).reference for kind in (
        "migration", "preservation", "browser", "destructive_no_mutation",
    ))
    report = compose_modification_gate_report(
        assessment, migration_ref=refs[0], preservation_ref=refs[1], browser_ref=refs[2], destructive_no_mutation_ref=refs[3],
    )
    coordinator = RunBoundModificationCoordinator(store)
    with pytest.raises(ValueError, match="migration_ref"):
        coordinator.bind_gate_approval(run.id, replace(report, migration_ref="run:wrong:evidence:missing"), "missing")
    wrong_gate = store.record_milestone_gate_approval(run.id, gate="LLM-GATE", approver="operator", approved=True, intent=run.intent)
    with pytest.raises(ValueError, match="exact M8"):
        coordinator.bind_gate_approval(run.id, report, wrong_gate.id)
    wrong_spec = store.record_milestone_gate_approval(
        run.id, gate="MOD-GATE", approver="operator", approved=True, intent=run.intent,
    )
    with pytest.raises(ValueError, match="exact M8"):
        coordinator.bind_gate_approval(run.id, replace(report, installed_spec_hash="0" * 64), wrong_spec.id)
    other = store.create_run(RunIntent(LockTarget("/other", "dev.local", "task_tracker"), plan.provider, plan.spec_hash, plan.plan_hash))
    store.transition(other.id, RunState.PENDING_APPROVAL)
    store.record_approval(other.id, approver="operator", approved=True, intent=other.intent)
    store.transition(other.id, RunState.APPROVED)
    store.transition(other.id, RunState.RUNNING)
    other_approval = store.record_milestone_gate_approval(other.id, gate="MOD-GATE", approver="operator", approved=True, intent=other.intent)
    with pytest.raises(ValueError, match="exact M8"):
        coordinator.bind_gate_approval(run.id, report, other_approval.id)
    durable = store.record_milestone_gate_approval(run.id, gate="MOD-GATE", approver="operator", approved=True, intent=run.intent)
    bound = coordinator.bind_gate_approval(run.id, report, durable.id)
    safe = store.record_lifecycle_evidence(run.id, "safe_modification", {"passed": True})
    destructive = store.record_lifecycle_evidence(run.id, "destructive_no_mutation", {"passed": True})
    decision = evaluate_milestone_gate_for_run(
        store, run.id, "MOD-GATE",
        evidence_refs={"safe_modification": safe.reference, "destructive_no_mutation": destructive.reference},
    )
    assert bound.status == "approved"
    assert decision.passed


def test_successor_v2_gate_binds_parent_assessment_and_successor_intent(tmp_path):
    installed, confirmed = _specs()
    _spec, plan, _compilation, store, parent, _approval, _backup, _checkpoint = _prepared_store(tmp_path)
    coordinator = RunBoundModificationCoordinator(store)
    assessment = coordinator.assess(parent.id, installed, confirmed, reconfirmed_spec_hash=spec_hash(confirmed))
    v2_plan = build_metadata_plan(confirmed, provider=plan.provider)
    successor = coordinator.create_revision_run(parent.id, assessment, plan=v2_plan)
    store.transition(successor.id, RunState.PENDING_APPROVAL)
    store.record_approval(successor.id, approver="operator", approved=True, intent=successor.intent)
    store.transition(successor.id, RunState.APPROVED)
    store.transition(successor.id, RunState.RUNNING)
    refs = {
        check: store.record_lifecycle_evidence(successor.id, f"modification_{check}", {
            "passed": True, "mutation_attempted": False,
        }).reference
        for check in ("migration", "preservation", "browser", "destructive_no_mutation")
    }
    affected = AffectedCheckEvidence(refs["migration"], refs["preservation"], refs["browser"], refs["destructive_no_mutation"])
    safe = coordinator.record_safe_modification_evidence(successor.id, assessment_ref=assessment.evidence_id and store.get_lifecycle_evidence(assessment.evidence_id).reference, affected=affected)
    report = compose_modification_gate_report(
        assessment.assessment,
        migration_ref=refs["migration"], preservation_ref=refs["preservation"], browser_ref=refs["browser"],
        destructive_no_mutation_ref=refs["destructive_no_mutation"], execution_spec_hash=successor.intent.spec_hash,
        assessment_ref=safe and store.get_lifecycle_evidence_payload(safe.id)["assessment_ref"],
    )
    approval = store.record_milestone_gate_approval(successor.id, gate="MOD-GATE", approver="operator", approved=True, intent=successor.intent)
    bound = coordinator.bind_gate_approval(successor.id, report, approval.id)
    assert bound.status == "approved"
    assert store.get_run(parent.id).state is RunState.CANCELLED
    assert store.get_run(successor.id).metadata["revision_of"] == parent.id


def test_revision_run_inherits_the_parent_immutable_budget(tmp_path):
    installed, confirmed = _specs()
    budget = OperationalBudget(command_timeout_seconds=17, max_log_bytes=4321)
    _spec, plan, _compilation, store, parent, _approval, _backup, _checkpoint = _prepared_store(tmp_path, budget=budget)
    assessment = RunBoundModificationCoordinator(store).assess(
        parent.id, installed, confirmed, reconfirmed_spec_hash=spec_hash(confirmed),
    )
    successor = RunBoundModificationCoordinator(store).create_revision_run(
        parent.id, assessment, plan=build_metadata_plan(confirmed, provider=plan.provider),
    )
    assert store.get_budget(successor.id) == budget


def test_successor_executor_runs_migration_checks_and_safe_summary(tmp_path):
    installed, confirmed = _specs()
    _spec, parent_plan, _parent_compilation, store, parent, _parent_approval, _backup, _checkpoint = _prepared_store(tmp_path)
    coordinator = RunBoundModificationCoordinator(store)
    assessment = coordinator.assess(parent.id, installed, confirmed, reconfirmed_spec_hash=spec_hash(confirmed))
    v2_plan = build_metadata_plan(confirmed, provider=parent_plan.provider)
    successor = coordinator.create_revision_run(parent.id, assessment, plan=v2_plan)
    store.transition(successor.id, RunState.PENDING_APPROVAL)
    approval = store.record_approval(successor.id, approver="operator", approved=True, intent=successor.intent)
    store.transition(successor.id, RunState.APPROVED)
    store.transition(successor.id, RunState.RUNNING)

    from frappe_harness.compiler import compile_project
    from frappe_harness.environment_preflight import (
        DiskHealth, EnvironmentIdentity, EnvironmentPreflight, FrappeRuntime, HealthCheck, SiteSafety, ToolStatus,
    )
    from frappe_harness.runtime_lifecycle import RunLifecycleCoordinator
    lifecycle = RunLifecycleCoordinator(store)
    lifecycle.record_preflight(successor.id, EnvironmentPreflight(
        EnvironmentIdentity(successor.target.bench, successor.target.site, successor.target.app),
        FrappeRuntime(16, "16.0"), SiteSafety(True, False), (),
        (ToolStatus("bench", True), ToolStatus("frappectl", True)),
        DiskHealth(2_000_000_000, True), (HealthCheck("database", True), HealthCheck("site", True)),
    ))
    compilation = compile_project(confirmed)
    lifecycle.record_compilation(successor.id, confirmed, compilation)
    lifecycle.record_plan_approval(successor.id, v2_plan, approval.id)
    command = store.start_command(successor.id, command="bench", arguments={"operation": "SiteBackup"})
    command = store.complete_command(command.id, exit_code=0)
    backup = store.record_lifecycle_evidence(successor.id, "backup", {
        "recovery_receipt": "v1", "command_receipt_id": command.id, "operation": "SiteBackup",
        "target": {"bench": successor.target.bench, "site": successor.target.site, "app": successor.target.app},
    })
    checkpoint = store.record_lifecycle_evidence(successor.id, "checkpoint", {
        "recovery_receipt": "v1", "provider_id": "source-checkpoint", "reference": "checkpoint:v2",
        "checkpoint": "def", "target": {"bench": successor.target.bench, "site": successor.target.site, "app": successor.target.app},
        "spec_hash": successor.intent.spec_hash, "plan_hash": successor.intent.plan_hash,
    })

    class Affected:
        def execute(self, run_id, check):
            result = {
                "passed": True,
                "target": {"bench": successor.target.bench, "site": successor.target.site, "app": successor.target.app},
                "spec_hash": successor.intent.spec_hash, "plan_hash": successor.intent.plan_hash,
            }
            return result | ({"mutation_attempted": False} if check == "destructive_no_mutation" else {})

    result = RunBoundSuccessorModificationExecutor(store).execute(
        successor.id,
        parent_assessment_ref=store.get_lifecycle_evidence(assessment.evidence_id).reference,
        installed=installed, confirmed=confirmed, plan=v2_plan, compilation=compilation,
        schema_diff=classify_schema_diff(installed, confirmed),
        approval_id=approval.id, backup_evidence_id=backup.id, checkpoint_evidence_id=checkpoint.id,
        migration_provider=FakeProvider(), affected_provider=Affected(),
    )
    assert result.migration_evidence.kind == "migration_receipt"
    assert result.safe_modification.kind == "safe_modification"
    assert result.affected.destructive_no_mutation_ref.startswith(f"run:{successor.id}:evidence:")
