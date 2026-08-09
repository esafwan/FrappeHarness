from __future__ import annotations

import json
from pathlib import Path

import pytest

from frappe_harness.run_store import LockTarget, RunIntent, RunState, RunStore
from frappe_harness.spec_io import load_project_spec
from frappe_harness.test_templates import generate_verification_templates
from frappe_harness.verification_execution import (
    RunBoundVerificationExecutor,
    VerificationExecutionAdmissionError,
    VerificationIntentBindingError,
)
from frappe_harness.verification_program import VerificationObservation, load_verification_program


@pytest.fixture
def store(tmp_path):
    return RunStore(tmp_path / "runs.sqlite3")


def _program():
    spec = load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")
    templates = generate_verification_templates(spec)
    return load_verification_program(json.loads(templates.artifacts["harness/verification-plan.json"]))


def _run(store, program, *, running=True):
    run = store.create_run(RunIntent(LockTarget("bench", "site", "app"), "frappe-native", program.spec_hash, "b" * 64))
    if not running:
        return run
    store.transition(run.id, RunState.PENDING_APPROVAL)
    store.record_approval(run.id, approver="safwan", approved=True, intent=run.intent)
    store.transition(run.id, RunState.APPROVED)
    return store.transition(run.id, RunState.RUNNING)


def test_run_bound_executor_persists_normalized_report_without_advancing_lifecycle(store):
    program = _program()
    run = _run(store, program)
    seen = []

    class Probe:
        def probe(self, request):
            seen.append(request)
            return VerificationObservation(request.case.expected)

    result = RunBoundVerificationExecutor(store).execute(run.id, program, Probe())

    assert result.report.passed
    assert len(seen) == len(program.cases)
    assert store.get_verification_report(result.report_record.id) == result.report_record
    assert store.get_lifecycle_snapshot(run.id) is None


def test_non_running_run_is_rejected_before_probe_invocation(store):
    program = _program()
    run = _run(store, program, running=False)
    calls = 0

    class Probe:
        def probe(self, request):
            nonlocal calls
            calls += 1
            return VerificationObservation(request.case.expected)

    with pytest.raises(VerificationExecutionAdmissionError, match="running"):
        RunBoundVerificationExecutor(store).execute(run.id, program, Probe())

    assert calls == 0


def test_mismatched_program_spec_is_rejected_before_probe_invocation(store):
    program = _program()
    other = store.create_run(RunIntent(LockTarget("other", "site", "app"), "frappe-native", "a" * 64, "b" * 64))
    store.transition(other.id, RunState.PENDING_APPROVAL)
    store.record_approval(other.id, approver="safwan", approved=True, intent=other.intent)
    store.transition(other.id, RunState.APPROVED)
    store.transition(other.id, RunState.RUNNING)
    calls = 0

    class Probe:
        def probe(self, request):
            nonlocal calls
            calls += 1
            return VerificationObservation(request.case.expected)

    with pytest.raises(VerificationIntentBindingError, match="approved spec"):
        RunBoundVerificationExecutor(store).execute(other.id, program, Probe())

    assert calls == 0


def test_failing_probe_result_is_persisted_but_never_advances_backend_lifecycle(store):
    program = _program()
    run = _run(store, program)

    class Probe:
        def probe(self, request):
            return VerificationObservation("wrong")

    result = RunBoundVerificationExecutor(store).execute(run.id, program, Probe())

    assert not result.report.passed
    assert store.get_verification_report(result.report_record.id) == result.report_record
    assert store.get_lifecycle_snapshot(run.id) is None
