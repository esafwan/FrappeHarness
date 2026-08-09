"""Run-bound backup and source-checkpoint evidence providers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .bench_command_policy import InspectedBenchTarget, SiteBackup
from .bench_execution import RunBoundBenchExecutor
from .run_store import CommandReceipt, LifecycleEvidenceRecord, LockTarget, RunStore


class RecoveryEvidenceError(RuntimeError):
    """Recovery evidence could not be proven for the exact run intent."""

    def __init__(self, message: str, *, after_backup: bool = False) -> None:
        super().__init__(message)
        self.after_backup = after_backup


@dataclass(frozen=True)
class SourceCheckpointReceipt:
    reference: str
    checkpoint: str
    target: LockTarget
    spec_hash: str
    plan_hash: str
    provider_id: str = "source-checkpoint"


class SourceCheckpointProvider(Protocol):
    def create(self, *, target: LockTarget, spec_hash: str, plan_hash: str) -> SourceCheckpointReceipt:
        """Return an immutable, already-inspected source checkpoint receipt."""


@dataclass(frozen=True)
class RecoveryEvidenceResult:
    backup: LifecycleEvidenceRecord
    checkpoint: LifecycleEvidenceRecord
    backup_command: CommandReceipt
    checkpoint_receipt: SourceCheckpointReceipt


class RunBoundRecoveryExecutor:
    """Create recovery evidence only from exact-target typed capabilities."""

    def __init__(self, store: RunStore, *, bench: RunBoundBenchExecutor | None = None) -> None:
        self.store = store
        self.bench = bench or RunBoundBenchExecutor(store)

    def execute(
        self,
        run_id: str,
        *,
        inspected_target: InspectedBenchTarget,
        checkpoint_provider: SourceCheckpointProvider,
    ) -> RecoveryEvidenceResult:
        run = self.store.get_run(run_id)
        target = LockTarget(inspected_target.bench_path, inspected_target.site_name, inspected_target.app_slug)
        if run.target != target:
            raise RecoveryEvidenceError("inspected recovery target does not match the run intent")
        backup_result = self.bench.execute(run_id, SiteBackup(inspected_target), inspected_target=inspected_target)
        command = backup_result.command_receipt
        if command.status != "completed" or command.exit_code != 0:
            raise RecoveryEvidenceError("site backup did not complete successfully")
        backup = self.store.record_lifecycle_evidence(run_id, "backup", {
            "recovery_receipt": "v1", "command_receipt_id": command.id,
            "operation": "SiteBackup", "target": {"bench": target.bench, "site": target.site, "app": target.app},
        })
        try:
            checkpoint = checkpoint_provider.create(target=target, spec_hash=run.intent.spec_hash, plan_hash=run.intent.plan_hash)
        except Exception as error:
            self._record_failure(run_id, backup.id, "checkpoint", "source checkpoint provider failed")
            raise RecoveryEvidenceError("source checkpoint provider failed", after_backup=True) from error
        if not isinstance(checkpoint, SourceCheckpointReceipt):
            self._record_failure(run_id, backup.id, "checkpoint", "source checkpoint provider returned an untyped receipt")
            raise RecoveryEvidenceError("source checkpoint provider returned an untyped receipt", after_backup=True)
        if (
            not checkpoint.reference.strip()
            or not checkpoint.checkpoint.strip()
            or not checkpoint.provider_id.strip()
        ):
            self._record_failure(run_id, backup.id, "checkpoint", "source checkpoint receipt is incomplete")
            raise RecoveryEvidenceError("source checkpoint receipt is incomplete", after_backup=True)
        if checkpoint.target != target or checkpoint.spec_hash != run.intent.spec_hash or checkpoint.plan_hash != run.intent.plan_hash:
            self._record_failure(run_id, backup.id, "checkpoint", "source checkpoint does not bind the run intent")
            raise RecoveryEvidenceError("source checkpoint does not bind the run intent", after_backup=True)
        checkpoint_evidence = self.store.record_lifecycle_evidence(run_id, "checkpoint", {
            "recovery_receipt": "v1", "provider_id": checkpoint.provider_id,
            "reference": checkpoint.reference, "checkpoint": checkpoint.checkpoint,
            "target": {"bench": target.bench, "site": target.site, "app": target.app},
            "spec_hash": checkpoint.spec_hash, "plan_hash": checkpoint.plan_hash,
        })
        return RecoveryEvidenceResult(backup, checkpoint_evidence, command, checkpoint)

    def _record_failure(
        self, run_id: str, backup_evidence_id: str, phase: str, reason: str,
    ) -> LifecycleEvidenceRecord:
        """Persist durable, non-fabricated evidence that recovery did not complete.

        The record explicitly attests that no automatic restore was attempted,
        keeping the policy fail-closed until an authorized operator reconciles.
        """
        return self.store.record_lifecycle_evidence(run_id, "recovery_failure", {
            "recovery_receipt": "v1",
            "backup_evidence_id": backup_evidence_id,
            "phase": phase,
            "reason": reason,
            "auto_restore_attempted": False,
        })
