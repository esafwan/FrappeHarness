"""Run-bound execution of a typed verification program.

This coordinator owns no process, HTTP, Bench, credential, or provider
capability.  It only admits an injected :class:`VerificationProbe` for the
exact approved run intent and persists the normalized report through the
append-only run ledger.  Backend lifecycle passage remains a separate,
explicit caller action after predecessors have been evidenced.
"""

from __future__ import annotations

from dataclasses import dataclass

from .run_store import (
    InvalidStateTransition,
    IntentMismatch,
    PersistedLifecycleSnapshot,
    RunState,
    RunStore,
    VerificationReportRecord,
)
from .verification_program import (
    VerificationProbe,
    VerificationProgram,
    VerificationRunReport,
    execute_verification_program,
)


class VerificationExecutionAdmissionError(RuntimeError):
    """A run cannot execute its verification program at this time."""


class VerificationIntentBindingError(VerificationExecutionAdmissionError):
    """The program does not bind to the run's immutable approved spec."""


@dataclass(frozen=True)
class RunBoundVerificationResult:
    """The in-memory report and its normalized append-only ledger identity."""

    report: VerificationRunReport
    report_record: VerificationReportRecord


@dataclass(frozen=True)
class RunBoundBackendVerificationResult:
    """A report bound to the backend lifecycle state when it passes."""

    report: VerificationRunReport
    report_record: VerificationReportRecord
    snapshot: PersistedLifecycleSnapshot | None


class RunBoundVerificationExecutor:
    """Execute a probe only after local run/intention admission checks pass."""

    def __init__(self, store: RunStore) -> None:
        self.store = store

    def execute(
        self, run_id: str, program: VerificationProgram, probe: VerificationProbe,
    ) -> RunBoundVerificationResult:
        """Execute and persist a report without changing lifecycle snapshot state."""

        run = self.store.get_run(run_id)
        if run.state is not RunState.RUNNING:
            raise VerificationExecutionAdmissionError("verification may execute only for a running approved run")
        if program.spec_hash != run.intent.spec_hash:
            raise VerificationIntentBindingError("verification program does not bind the run's approved spec")
        report = execute_verification_program(program, probe)
        try:
            report_record = self.store.record_verification_report(run_id, program, report)
        except IntentMismatch as error:
            raise VerificationIntentBindingError(str(error)) from error
        except InvalidStateTransition as error:
            # A concurrent state transition after preflight must not turn into
            # a probe retry or an implicit lifecycle advancement.
            raise VerificationExecutionAdmissionError(str(error)) from error
        return RunBoundVerificationResult(report, report_record)

    def execute_backend(
        self, run_id: str, program: VerificationProgram, probe: VerificationProbe,
    ) -> RunBoundBackendVerificationResult:
        """Execute a program and advance ``BACKEND_VERIFIED`` only when all cases pass."""
        run = self.store.get_run(run_id)
        if run.state is not RunState.RUNNING:
            raise VerificationExecutionAdmissionError("verification may execute only for a running approved run")
        if program.spec_hash != run.intent.spec_hash:
            raise VerificationIntentBindingError("verification program does not bind the run's approved spec")
        report = execute_verification_program(program, probe)
        if report.passed:
            report_record, snapshot = self.store.record_backend_verification(run_id, program, report)
            return RunBoundBackendVerificationResult(report, report_record, snapshot)
        report_record = self.store.record_verification_report(run_id, program, report)
        return RunBoundBackendVerificationResult(report, report_record, None)
