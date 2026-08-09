"""Run-bound execution of the narrow, typed Bench operation allowlist.

This is the only orchestration bridge between a durable run and
``BenchRunner``.  It creates a digest-only receipt before starting a child
process, prevents a command from escaping its approved target, and leaves
schema mutation and interactive scaffolding outside this early execution
slice.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib

from .bench_command_policy import (
    BenchOperation,
    InspectedBenchTarget,
    NewAppScaffold,
    SiteMigrate,
    build_bench_argv,
)
from .bench_runner import BenchCommandTimedOut, BenchExecutionResult, BenchOutputTooLarge, BenchRunner, BenchToolUnavailable
from .operational_budget import OperationalBudget
from .run_store import CommandReceipt, LockTarget, RunState, RunStore


class BenchExecutionAdmissionError(RuntimeError):
    """A run is not admitted to execute a typed Bench operation."""


class BenchTargetBindingError(BenchExecutionAdmissionError):
    """The operation target differs from the immutable target of its run."""


class DeferredBenchOperation(BenchExecutionAdmissionError):
    """The operation needs a later, more specific orchestration gate."""


@dataclass(frozen=True)
class RunBoundBenchExecutionResult:
    """The in-memory result and its durable, output-free command receipt."""

    execution: BenchExecutionResult
    command_receipt: CommandReceipt


class RunBoundBenchExecutor:
    """Execute a typed operation only for its exact active, locked run."""

    def __init__(self, store: RunStore, *, runner: BenchRunner | None = None, budget: OperationalBudget | None = None) -> None:
        self.store = store
        if runner is not None and budget is not None:
            raise ValueError("provide runner or budget, not both")
        # Keep explicit injection backwards compatible, but defer the default
        # runner until execute(): the immutable budget is bound to a run, not
        # to this reusable executor instance.
        self.runner = runner
        self.budget = budget

    def execute(
        self,
        run_id: str,
        operation: BenchOperation,
        *,
        inspected_target: InspectedBenchTarget,
    ) -> RunBoundBenchExecutionResult:
        """Create a durable receipt, run an admitted operation, then finalize it.

        A migration must pass the full pre-migration gate with environment
        verified evidence; an interactive ``bench new-app`` cannot be used by
        a headless harness.  Both are deliberately rejected before a receipt
        or subprocess is created.
        """

        if isinstance(operation, (SiteMigrate, NewAppScaffold)):
            raise DeferredBenchOperation(
                "migration and interactive new-app scaffolding require a later dedicated orchestration path"
            )
        argv = build_bench_argv(operation, inspected_target=inspected_target)
        run = self.store.get_run(run_id)
        target = LockTarget(inspected_target.bench_path, inspected_target.site_name, inspected_target.app_slug)
        if run.target != target:
            raise BenchTargetBindingError("inspected Bench target does not match the run's immutable target")
        if run.state is not RunState.RUNNING:
            raise BenchExecutionAdmissionError("Bench operations may execute only for a running approved run")
        runner = self.runner
        if runner is None:
            # Resolve and validate the immutable budget before recording a
            # command attempt, so a tampered/invalid budget cannot strand a
            # started receipt.
            runner = BenchRunner(budget=self.budget if self.budget is not None else self.store.get_budget(run_id))

        receipt = self.store.start_command(
            run_id,
            command="bench",
            arguments={"operation": type(operation).__name__, "argv": list(argv)},
        )
        try:
            execution = runner.run(operation, inspected_target=inspected_target)
        except (BenchToolUnavailable, BenchCommandTimedOut, BenchOutputTooLarge):
            self.store.complete_command_summary(
                receipt.id,
                exit_code=None,
                stdout_bytes=0,
                stdout_sha256=_EMPTY_SHA256,
                stderr_bytes=0,
                stderr_sha256=_EMPTY_SHA256,
            )
            raise
        completed = self.store.complete_command_summary(
            receipt.id,
            exit_code=execution.receipt.exit_code,
            stdout_bytes=execution.receipt.stdout_bytes,
            stdout_sha256=execution.receipt.stdout_sha256,
            stderr_bytes=execution.receipt.stderr_bytes,
            stderr_sha256=execution.receipt.stderr_sha256,
        )
        return RunBoundBenchExecutionResult(execution, completed)


_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
