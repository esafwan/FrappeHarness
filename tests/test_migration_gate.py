from dataclasses import replace
from pathlib import Path

from frappe_harness.compiler import CompilationResult, compile_project
from frappe_harness.migration_gate import (
    MigrationEvidence,
    MigrationGateInput,
    evaluate_pre_migration_gate,
)
from frappe_harness.provider_plan import build_metadata_plan
from frappe_harness.run_store import ApprovalRecord, LockTarget, RunIntent
from frappe_harness.schema_diff import classify_schema_diff
from frappe_harness.spec_io import load_project_spec


def _parts():
    spec = load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")
    plan = build_metadata_plan(spec)
    intent = RunIntent(LockTarget("/bench", "dev.local", "task_tracker"), plan.provider, plan.spec_hash, plan.plan_hash)
    approval = ApprovalRecord("approval-1", "run-1", "operator", "approved", None, "2026-01-01T00:00:00Z", intent)
    return spec, plan, intent, approval


def _input(**changes):
    spec, plan, intent, approval = _parts()
    data = {
        "run_id": "run-1",
        "intent": intent,
        "plan": plan,
        "compilation": compile_project(spec),
        "schema_diff": classify_schema_diff(spec, spec),
        "approval": approval,
        "evidence": MigrationEvidence("backup-1", "git:abc123"),
    }
    data.update(changes)
    return MigrationGateInput(**data)


def test_gate_allows_only_fully_bound_clean_inputs():
    decision = evaluate_pre_migration_gate(_input())
    assert decision.allowed
    assert decision.failures == ()
    assert decision.artifact_audit.valid


def test_gate_fails_closed_for_missing_or_wrong_approval_and_evidence():
    decision = evaluate_pre_migration_gate(_input(approval=None, evidence=None))
    assert not decision.allowed
    assert set(decision.failure_codes) >= {"missing_approval", "missing_migration_evidence"}

    _, _, intent, approval = _parts()
    wrong_intent = replace(intent, plan_hash="f" * 64)
    wrong_approval = replace(approval, run_id="other-run", intent=wrong_intent, decision="rejected", approver=" ")
    decision = evaluate_pre_migration_gate(_input(approval=wrong_approval))
    assert not decision.allowed
    assert set(decision.failure_codes) >= {"approval_run_mismatch", "approval_intent_mismatch", "approval_not_approved", "missing_approver"}


def test_gate_blocks_schema_drift_and_hash_mismatches():
    spec, plan, intent, _ = _parts()
    task = next(entity for entity in spec.entities if entity.name == "task")
    changed_task = replace(task, fields=task.fields[:-1])
    changed = replace(spec, entities=tuple(changed_task if entity.name == "task" else entity for entity in spec.entities), screens=tuple(replace(screen, fields=tuple(field for field in screen.fields if field != "due_date")) if screen.entity == "task" else screen for screen in spec.screens))
    decision = evaluate_pre_migration_gate(_input(schema_diff=classify_schema_diff(spec, changed)))
    assert not decision.allowed
    assert set(decision.failure_codes) >= {"blocked_schema_diff", "diff_spec_hash_mismatch"}

    decision = evaluate_pre_migration_gate(_input(intent=replace(intent, plan_hash="f" * 64)))
    assert not decision.allowed
    assert "plan_hash_mismatch" in decision.failure_codes


def test_gate_carries_audit_failures_into_structured_blockers():
    input = _input()
    artifacts = dict(input.compilation.artifacts)
    artifacts["task_tracker/fixtures/roles.json"] = b"[]\n"
    tampered = CompilationResult(artifacts, input.compilation.ownership_manifest, input.compilation.spec_hash)
    decision = evaluate_pre_migration_gate(replace(input, compilation=tampered))
    assert not decision.allowed
    assert any(code.startswith("artifact_") for code in decision.failure_codes)


def test_gate_requires_diff_identity_and_nonblank_backup_checkpoint():
    input = _input()
    unbound_diff = replace(input.schema_diff, installed_spec_hash=None, confirmed_spec_hash=None)
    decision = evaluate_pre_migration_gate(replace(input, schema_diff=unbound_diff, evidence=MigrationEvidence("", "")))
    assert not decision.allowed
    assert set(decision.failure_codes) >= {"diff_spec_hash_mismatch", "missing_installed_schema_identity", "missing_backup", "missing_checkpoint"}
