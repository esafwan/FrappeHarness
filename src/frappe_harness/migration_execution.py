"""Run-bound execution for one approved, schema-changing provider plan.

The core owns admission and evidence.  A provider is an injected typed
capability: it receives an immutable target and semantic plan, never a raw
command or model-authored path, and returns only redacted execution evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Protocol

from .compiler import CompilationResult
from .lifecycle_contract import LifecycleState
from .provider_plan import MetadataPlan
from .run_store import (
    AttemptOutcome,
    CommandReceipt,
    InvalidStateTransition,
    LockTarget,
    RunState,
    RunStore,
    LifecycleEvidenceRecord,
    PersistedLifecycleSnapshot,
)
from .runtime_lifecycle import RunLifecycleCoordinator, RuntimeLifecycleError
from .schema_diff import SchemaDiffReport


class MigrationExecutionAdmissionError(RuntimeError):
    """The exact run is not admitted to provider mutation."""


class MigrationProviderError(RuntimeError):
    """The injected provider failed before producing a successful receipt."""


@dataclass(frozen=True)
class MigrationProviderReceipt:
    """Digest-only evidence returned by a typed mutation provider."""

    reference: str
    exit_code: int
    stdout_bytes: int = 0
    stdout_sha256: str = ""
    stderr_bytes: int = 0
    stderr_sha256: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.reference, str) or not self.reference.strip():
            raise ValueError("migration receipt reference must be non-blank")
        if isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int):
            raise ValueError("migration receipt exit_code must be an integer")
        for label, value in (("stdout_bytes", self.stdout_bytes), ("stderr_bytes", self.stderr_bytes)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{label} must be a non-negative integer")
        empty = hashlib.sha256(b"").hexdigest()
        for label, value in (("stdout_sha256", self.stdout_sha256), ("stderr_sha256", self.stderr_sha256)):
            if value == "":
                object.__setattr__(self, label, empty)
            elif not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"{label} must be a 64-character lower-case SHA-256 digest")


class MigrationProvider(Protocol):
    """Typed mutation capability supplied by a trusted provider adapter."""

    provider_id: str

    def apply(self, *, target: LockTarget, plan: MetadataPlan, compilation: CompilationResult) -> MigrationProviderReceipt:
        """Apply the already-approved semantic plan to the exact target."""


@dataclass(frozen=True)
class RunBoundMigrationResult:
    receipt: MigrationProviderReceipt
    command_receipt: CommandReceipt
    evidence: LifecycleEvidenceRecord
    snapshot: PersistedLifecycleSnapshot


class RunBoundMigrationExecutor:
    """Admit, execute, and evidence one exact migration mutation."""

    def __init__(self, store: RunStore) -> None:
        self.store = store
        self.coordinator = RunLifecycleCoordinator(store)

    def execute(
        self,
        run_id: str,
        *,
        plan: MetadataPlan,
        compilation: CompilationResult,
        schema_diff: SchemaDiffReport,
        approval_id: str,
        backup_evidence_id: str,
        checkpoint_evidence_id: str,
        provider: MigrationProvider,
    ) -> RunBoundMigrationResult:
        """Run the migration only after all immutable gate inputs agree.

        A provider exception leaves the command receipt failed and does not
        advance lifecycle state.  A process interruption leaves a started
        receipt, which deliberately prevents silent replay or terminalization.
        """
        run = self.store.get_run(run_id)
        if run.state is not RunState.RUNNING:
            raise MigrationExecutionAdmissionError("migration may execute only for a running approved run")
        if getattr(provider, "provider_id", None) != run.intent.provider:
            raise MigrationExecutionAdmissionError("provider does not match the run's approved provider")
        # Providers may opt into per-run budget binding without changing the
        # narrow ``MigrationProvider.apply`` protocol. Custom providers that
        # do not expose this capability remain fully backwards compatible.
        bound_provider = provider
        bind_budget = getattr(provider, "bind_budget", None)
        if callable(bind_budget):
            try:
                bound_provider = bind_budget(self.store.get_budget(run_id))
            except Exception as error:
                raise MigrationExecutionAdmissionError("migration provider could not bind the run budget") from error
            if getattr(bound_provider, "provider_id", None) != run.intent.provider:
                raise MigrationExecutionAdmissionError("budget-bound provider does not match the run's approved provider")
        if plan.spec_hash != run.intent.spec_hash or plan.plan_hash != run.intent.plan_hash:
            raise MigrationExecutionAdmissionError("provider plan does not bind the run's immutable intent")
        if compilation.spec_hash != run.intent.spec_hash:
            raise MigrationExecutionAdmissionError("compiled artifacts do not bind the run's immutable intent")
        snapshot = self.store.get_lifecycle_snapshot(run_id)
        if snapshot is not None and snapshot.snapshot.record_for(_MIGRATION_APPLIED).status.value == "passed":
            raise MigrationExecutionAdmissionError("migration has already been applied for this run")
        if snapshot is not None and snapshot.snapshot.record_for(_MIGRATION_APPLIED).status.value in {"running", "interrupted"}:
            raise MigrationExecutionAdmissionError("interrupted mutation requires reconciliation before replay")

        # Re-evaluate the gate on every process restart.  This makes the
        # provider call depend on the same exact evidence, even if the gate was
        # recorded by an earlier process.
        if snapshot is not None and snapshot.snapshot.record_for(_MIGRATION_GATED).status.value == "passed":
            gate_snapshot = snapshot
        else:
            try:
                gate_snapshot = self.coordinator.record_migration_gate(
                    run_id, plan, compilation, schema_diff, approval_id,
                    backup_evidence_id, checkpoint_evidence_id,
                )
            except (RuntimeLifecycleError, InvalidStateTransition) as error:
                raise MigrationExecutionAdmissionError(str(error)) from error

        # Persist the mutation attempt before invoking the provider.  If the
        # process dies after this point, resume logic can distinguish a
        # potentially-partial mutation from a retryable validation failure.
        self.store.record_lifecycle_attempt(
            run_id, _MIGRATION_APPLIED, AttemptOutcome.STARTED,
        )
        command = self.store.start_command(
            run_id,
            command="migration-provider",
            arguments={"provider": run.intent.provider, "plan_hash": plan.plan_hash, "target": {
                "bench": run.target.bench, "site": run.target.site, "app": run.target.app,
            }},
        )
        try:
            receipt = bound_provider.apply(target=run.target, plan=plan, compilation=compilation)
            if receipt.exit_code != 0:
                completed = self.store.complete_command_summary(
                    command.id, exit_code=receipt.exit_code,
                    stdout_bytes=receipt.stdout_bytes, stdout_sha256=receipt.stdout_sha256,
                    stderr_bytes=receipt.stderr_bytes, stderr_sha256=receipt.stderr_sha256,
                )
                self.store.record_lifecycle_attempt(
                    run_id, _MIGRATION_APPLIED, AttemptOutcome.FAILED,
                    failure_code="provider_nonzero_exit",
                )
                raise MigrationProviderError("migration provider returned a non-zero exit code")
            completed = self.store.complete_command_summary(
                command.id, exit_code=receipt.exit_code,
                stdout_bytes=receipt.stdout_bytes, stdout_sha256=receipt.stdout_sha256,
                stderr_bytes=receipt.stderr_bytes, stderr_sha256=receipt.stderr_sha256,
            )
        except MigrationProviderError:
            raise
        except Exception as error:
            # None is intentionally not a success code; if the provider may
            # have mutated before raising, the started/failed receipt is the
            # reconciliation signal and lifecycle remains at MIGRATION_GATED.
            try:
                self.store.complete_command_summary(
                    command.id, exit_code=None,
                    stdout_bytes=0, stdout_sha256=_EMPTY_SHA256,
                    stderr_bytes=0, stderr_sha256=_EMPTY_SHA256,
                )
            except Exception:
                pass
            self.store.record_lifecycle_attempt(
                run_id, _MIGRATION_APPLIED, AttemptOutcome.INTERRUPTED,
            )
            raise MigrationProviderError("migration provider failed") from error

        evidence = self.store.record_lifecycle_evidence(run_id, "migration_receipt", {
            "provider": run.intent.provider,
            "plan_hash": plan.plan_hash,
            "command_receipt_id": completed.id,
            "provider_reference": receipt.reference,
            "gate_revision": gate_snapshot.revision,
        })
        snapshot = self.store.advance_lifecycle_state(run_id, _MIGRATION_APPLIED, (evidence.id,))
        return RunBoundMigrationResult(receipt, completed, evidence, snapshot)

_MIGRATION_GATED = LifecycleState.MIGRATION_GATED
_MIGRATION_APPLIED = LifecycleState.MIGRATION_APPLIED
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
