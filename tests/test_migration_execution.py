from __future__ import annotations

from pathlib import Path
from dataclasses import replace

import pytest

from frappe_harness.compiler import compile_project
from frappe_harness.contracts import FieldType
from frappe_harness.lifecycle_contract import LifecycleState
from frappe_harness.environment_preflight import (
    DiskHealth, EnvironmentIdentity, EnvironmentPreflight, FrappeRuntime, HealthCheck, SiteSafety, ToolStatus,
)
from frappe_harness.migration_execution import (
    MigrationExecutionAdmissionError,
    MigrationProviderError,
    MigrationProviderReceipt,
    RunBoundMigrationExecutor,
)
from frappe_harness.provider_plan import build_metadata_plan
from frappe_harness.run_store import InvalidStateTransition, LockTarget, RunIntent, RunState, RunStore
from frappe_harness.schema_diff import classify_schema_diff
from frappe_harness.runtime_lifecycle import RunLifecycleCoordinator
from frappe_harness.spec_io import load_project_spec
from frappe_harness.bench_command_policy import InspectedBenchTarget
from frappe_harness.bench_migration_provider import BenchMigrationProvider
from frappe_harness.operational_budget import OperationalBudget


class FakeProvider:
    provider_id = "frappe-native"

    def __init__(self, *, exit_code: int = 0, explode: bool = False) -> None:
        self.exit_code = exit_code
        self.explode = explode
        self.calls = 0

    def apply(self, *, target, plan, compilation):
        self.calls += 1
        if self.explode:
            raise RuntimeError("provider interrupted")
        return MigrationProviderReceipt("provider:migration:1", self.exit_code)


class BindableFakeProvider(FakeProvider):
    def bind_budget(self, budget):
        self.bound_budget = budget
        return self


def _prepared_store(tmp_path, *, budget=None):
    spec = load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")
    plan = build_metadata_plan(spec)
    compilation = compile_project(spec)
    store = RunStore(tmp_path / "runs.sqlite3")
    run = store.create_run(
        RunIntent(LockTarget("/bench", "dev.local", "task_tracker"), plan.provider, plan.spec_hash, plan.plan_hash),
        budget=budget,
    )
    store.transition(run.id, RunState.PENDING_APPROVAL)
    approval = store.record_approval(run.id, approver="operator", approved=True, intent=run.intent)
    store.transition(run.id, RunState.APPROVED)
    store.transition(run.id, RunState.RUNNING)
    coordinator = RunLifecycleCoordinator(store)
    coordinator.record_preflight(run.id, EnvironmentPreflight(
        EnvironmentIdentity("/bench", "dev.local", "task_tracker"), FrappeRuntime(16, "16.0"),
        SiteSafety(True, False), (), (ToolStatus("bench", True), ToolStatus("frappectl", True)),
        DiskHealth(2_000_000_000, True), (HealthCheck("database", True), HealthCheck("site", True)),
    ))
    coordinator.record_compilation(run.id, spec, compilation)
    coordinator.record_plan_approval(run.id, plan, approval.id)
    command = store.start_command(run.id, command="bench", arguments={"operation": "SiteBackup"})
    command = store.complete_command(command.id, exit_code=0)
    backup = store.record_lifecycle_evidence(run.id, "backup", {"recovery_receipt": "v1", "command_receipt_id": command.id, "operation": "SiteBackup", "target": {"bench": run.target.bench, "site": run.target.site, "app": run.target.app}})
    checkpoint = store.record_lifecycle_evidence(run.id, "checkpoint", {"recovery_receipt": "v1", "provider_id": "source-checkpoint", "reference": "checkpoint:1", "checkpoint": "abc", "target": {"bench": run.target.bench, "site": run.target.site, "app": run.target.app}, "spec_hash": run.intent.spec_hash, "plan_hash": run.intent.plan_hash})
    return spec, plan, compilation, store, run, approval, backup, checkpoint


def test_run_bound_migration_executes_and_passes_state(tmp_path):
    spec, plan, compilation, store, run, approval, backup, checkpoint = _prepared_store(tmp_path)
    provider = FakeProvider()
    result = RunBoundMigrationExecutor(store).execute(
        run.id, plan=plan, compilation=compilation, schema_diff=classify_schema_diff(spec, spec),
        approval_id=approval.id, backup_evidence_id=backup.id, checkpoint_evidence_id=checkpoint.id,
        provider=provider,
    )

    assert provider.calls == 1
    assert result.command_receipt.status == "completed"
    assert result.snapshot.snapshot.record_for(LifecycleState.MIGRATION_APPLIED).status.value == "passed"
    assert result.evidence.kind == "migration_receipt"


def test_run_bound_migration_binds_persisted_budget_for_opt_in_provider(tmp_path):
    budget = OperationalBudget(command_timeout_seconds=7, max_log_bytes=1234)
    spec, plan, compilation, store, run, approval, backup, checkpoint = _prepared_store(tmp_path, budget=budget)
    provider = BindableFakeProvider()
    RunBoundMigrationExecutor(store).execute(
        run.id, plan=plan, compilation=compilation, schema_diff=classify_schema_diff(spec, spec),
        approval_id=approval.id, backup_evidence_id=backup.id, checkpoint_evidence_id=checkpoint.id,
        provider=provider,
    )

    assert provider.bound_budget == budget


def test_provider_failure_never_passes_migration_and_is_not_replayed_implicitly(tmp_path):
    spec, plan, compilation, store, run, approval, backup, checkpoint = _prepared_store(tmp_path)
    provider = FakeProvider(explode=True)
    with pytest.raises(MigrationProviderError):
        RunBoundMigrationExecutor(store).execute(
            run.id, plan=plan, compilation=compilation, schema_diff=classify_schema_diff(spec, spec),
            approval_id=approval.id, backup_evidence_id=backup.id, checkpoint_evidence_id=checkpoint.id,
            provider=provider,
        )
    snapshot = store.get_lifecycle_snapshot(run.id)
    assert snapshot is not None
    assert snapshot.snapshot.record_for(LifecycleState.MIGRATION_GATED).status.value == "passed"
    assert snapshot.snapshot.record_for(LifecycleState.MIGRATION_APPLIED).status.value == "interrupted"
    with pytest.raises(MigrationExecutionAdmissionError, match="requires reconciliation"):
        RunBoundMigrationExecutor(store).execute(
            run.id, plan=plan, compilation=compilation, schema_diff=classify_schema_diff(spec, spec),
            approval_id=approval.id, backup_evidence_id=backup.id, checkpoint_evidence_id=checkpoint.id,
            provider=FakeProvider(),
        )


def test_nonzero_provider_receipt_is_failed(tmp_path):
    spec, plan, compilation, store, run, approval, backup, checkpoint = _prepared_store(tmp_path)
    with pytest.raises(MigrationProviderError):
        RunBoundMigrationExecutor(store).execute(
            run.id, plan=plan, compilation=compilation, schema_diff=classify_schema_diff(spec, spec),
            approval_id=approval.id, backup_evidence_id=backup.id, checkpoint_evidence_id=checkpoint.id,
            provider=FakeProvider(exit_code=2),
        )
    assert store.get_lifecycle_snapshot(run.id).snapshot.record_for(LifecycleState.MIGRATION_APPLIED).status.value == "failed"


def test_wrong_provider_is_rejected_before_provider_call(tmp_path):
    spec, plan, compilation, store, run, approval, backup, checkpoint = _prepared_store(tmp_path)
    provider = FakeProvider()
    provider.provider_id = "commander-spike"
    with pytest.raises(MigrationExecutionAdmissionError, match="provider"):
        RunBoundMigrationExecutor(store).execute(
            run.id, plan=plan, compilation=compilation, schema_diff=classify_schema_diff(spec, spec),
            approval_id=approval.id, backup_evidence_id=backup.id, checkpoint_evidence_id=checkpoint.id,
            provider=provider,
        )
    assert provider.calls == 0


def test_blocked_destructive_diff_never_invokes_mutation_provider(tmp_path):
    spec, plan, compilation, store, run, approval, backup, checkpoint = _prepared_store(tmp_path)
    entity = spec.entities[1]
    destructive_entity = replace(entity, fields=tuple(
        replace(field, field_type=FieldType.DATA, options=()) if field.name == "priority" else field
        for field in entity.fields
    ))
    destructive = replace(spec, entities=tuple(
        destructive_entity if item.name == entity.name else item for item in spec.entities
    ))
    provider = FakeProvider()
    with pytest.raises(MigrationExecutionAdmissionError, match="blocked"):
        RunBoundMigrationExecutor(store).execute(
            run.id, plan=plan, compilation=compilation,
            schema_diff=classify_schema_diff(spec, destructive),
            approval_id=approval.id, backup_evidence_id=backup.id, checkpoint_evidence_id=checkpoint.id,
            provider=provider,
        )
    assert provider.calls == 0
    snapshot = store.get_lifecycle_snapshot(run.id)
    assert snapshot is None or snapshot.snapshot.record_for(LifecycleState.MIGRATION_APPLIED).status.value != "passed"


def test_bench_migration_provider_accepts_the_explicit_budget_envelope():
    target = InspectedBenchTarget("/bench", "dev.local", "task_tracker")
    budget = OperationalBudget(command_timeout_seconds=7, max_log_bytes=1234)
    provider = BenchMigrationProvider(target, budget=budget)
    assert provider.runner.budget == budget
    with pytest.raises(ValueError, match="runner or budget"):
        BenchMigrationProvider(target, runner=provider.runner, budget=budget)
