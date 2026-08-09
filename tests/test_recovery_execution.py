from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from frappe_harness.bench_command_policy import InspectedBenchTarget
from frappe_harness.bench_execution import RunBoundBenchExecutionResult
from frappe_harness.migration_execution import _EMPTY_SHA256
from frappe_harness.provider_plan import build_metadata_plan
from frappe_harness.recovery_execution import RecoveryEvidenceError, RunBoundRecoveryExecutor, SourceCheckpointReceipt
from frappe_harness.run_store import CommandReceipt, LockTarget, RunIntent, RunState, RunStore
from frappe_harness.spec_io import load_project_spec


class FakeBench:
    def __init__(self, store: RunStore, *, exit_code: int = 0):
        self.store, self.exit_code = store, exit_code

    def execute(self, run_id, operation, *, inspected_target):
        receipt = self.store.start_command(run_id, command="bench", arguments={"operation": type(operation).__name__})
        completed = self.store.complete_command_summary(receipt.id, exit_code=self.exit_code, stdout_bytes=0, stdout_sha256=_EMPTY_SHA256, stderr_bytes=0, stderr_sha256=_EMPTY_SHA256)
        return RunBoundBenchExecutionResult(None, completed)


class Checkpoint:
    def __init__(self, target, spec_hash, plan_hash):
        self.target, self.spec_hash, self.plan_hash = target, spec_hash, plan_hash

    def create(self, *, target, spec_hash, plan_hash):
        return SourceCheckpointReceipt("checkpoint:1", "abc123", target, spec_hash, plan_hash)


class BrokenCheckpoint:
    def create(self, *, target, spec_hash, plan_hash):
        raise RuntimeError("transport detail must not escape")


class UntypedCheckpoint:
    def create(self, *, target, spec_hash, plan_hash):
        return {"reference": "checkpoint:1"}


def _run(tmp_path):
    spec = load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")
    plan = build_metadata_plan(spec)
    store = RunStore(tmp_path / "runs.sqlite3")
    run = store.create_run(RunIntent(LockTarget("/bench", "dev.local", "task_tracker"), plan.provider, plan.spec_hash, plan.plan_hash))
    store.transition(run.id, RunState.PENDING_APPROVAL)
    store.record_approval(run.id, approver="operator", approved=True, intent=run.intent)
    store.transition(run.id, RunState.APPROVED)
    return store, store.transition(run.id, RunState.RUNNING), plan


def _failure_evidence_for_run(store: RunStore, run_id: str):
    with sqlite3.connect(store.database) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM lifecycle_evidence WHERE run_id = ? AND kind = ? ORDER BY created_at",
            (run_id, "recovery_failure"),
        ).fetchall()


def test_recovery_executor_persists_only_typed_success_receipts(tmp_path):
    store, run, plan = _run(tmp_path)
    target = InspectedBenchTarget("/bench", "dev.local", "task_tracker")
    result = RunBoundRecoveryExecutor(store, bench=FakeBench(store)).execute(
        run.id, inspected_target=target, checkpoint_provider=Checkpoint(run.target, plan.spec_hash, plan.plan_hash),
    )
    assert result.backup.kind == "backup"
    assert result.backup_command.exit_code == 0
    assert store.get_lifecycle_evidence_payload(result.checkpoint.id)["spec_hash"] == plan.spec_hash


def test_recovery_executor_rejects_nonzero_backup_without_checkpoint(tmp_path):
    store, run, plan = _run(tmp_path)
    target = InspectedBenchTarget("/bench", "dev.local", "task_tracker")
    with pytest.raises(RecoveryEvidenceError, match="backup") as exc_info:
        RunBoundRecoveryExecutor(store, bench=FakeBench(store, exit_code=1)).execute(
            run.id, inspected_target=target, checkpoint_provider=Checkpoint(run.target, plan.spec_hash, plan.plan_hash),
        )
    assert exc_info.value.after_backup is False


@pytest.mark.parametrize("provider", [BrokenCheckpoint(), UntypedCheckpoint()])
def test_recovery_executor_rejects_untyped_or_failed_checkpoint(tmp_path, provider):
    store, run, plan = _run(tmp_path)
    target = InspectedBenchTarget("/bench", "dev.local", "task_tracker")
    with pytest.raises(RecoveryEvidenceError, match="checkpoint") as exc_info:
        RunBoundRecoveryExecutor(store, bench=FakeBench(store)).execute(
            run.id, inspected_target=target, checkpoint_provider=provider,
        )
    assert exc_info.value.after_backup is True
    failures = _failure_evidence_for_run(store, run.id)
    assert len(failures) == 1
    payload = store.get_lifecycle_evidence_payload(failures[0]["id"])
    assert payload["phase"] == "checkpoint"
    assert payload["backup_evidence_id"]
    assert payload["auto_restore_attempted"] is False
