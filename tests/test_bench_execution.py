from __future__ import annotations

import hashlib
import subprocess

import pytest

from frappe_harness.bench_command_policy import (
    BuildApp,
    InspectedBenchTarget,
    NewAppScaffold,
    SiteMigrate,
)
from frappe_harness.bench_execution import (
    BenchExecutionAdmissionError,
    BenchTargetBindingError,
    DeferredBenchOperation,
    RunBoundBenchExecutor,
)
from frappe_harness.bench_runner import BenchCommandTimedOut, BenchOutputTooLarge, BenchRunner
from frappe_harness.operational_budget import OperationalBudget
from frappe_harness.run_store import LockTarget, RunIntent, RunState, RunStore


@pytest.fixture
def target() -> InspectedBenchTarget:
    return InspectedBenchTarget("/srv/frappe/harness", "harness.local", "task_tracker")


@pytest.fixture
def store(tmp_path) -> RunStore:
    return RunStore(tmp_path / "runs.sqlite3")


def _running_run(store: RunStore, target: InspectedBenchTarget, *, budget: OperationalBudget | None = None):
    intent = RunIntent(
        LockTarget(target.bench_path, target.site_name, target.app_slug),
        "frappe-native",
        "a" * 64,
        "b" * 64,
    )
    run = store.create_run(intent, budget=budget)
    store.transition(run.id, RunState.PENDING_APPROVAL)
    store.record_approval(run.id, approver="operator", approved=True, intent=intent)
    store.transition(run.id, RunState.APPROVED)
    return store.transition(run.id, RunState.RUNNING)


def test_executor_binds_typed_operation_to_running_approved_run_and_persists_redacted_receipt(monkeypatch, store, target):
    run = _running_run(store, target)
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, b"ordinary output", b"sensitive diagnostic")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = RunBoundBenchExecutor(store, runner=BenchRunner(timeout_seconds=9)).execute(
        run.id, BuildApp(target), inspected_target=target
    )

    assert seen["argv"] == ("bench", "build", "--app", "task_tracker")
    assert seen["kwargs"]["shell"] is False
    assert result.command_receipt.status == "completed"
    assert result.command_receipt.arguments == {
        "operation": "BuildApp", "argv": ["bench", "build", "--app", "task_tracker"],
    }
    assert result.command_receipt.stdout_sha256 == hashlib.sha256(b"ordinary output").hexdigest()
    assert result.command_receipt.stderr_sha256 == hashlib.sha256(b"sensitive diagnostic").hexdigest()
    assert not hasattr(result.command_receipt, "stdout")
    assert not hasattr(result.command_receipt, "stderr")


def test_executor_rejects_non_running_and_nonmatching_target_before_subprocess(monkeypatch, store, target):
    intent = RunIntent(LockTarget(target.bench_path, target.site_name, target.app_slug), "frappe-native", "a" * 64, "b" * 64)
    draft = store.create_run(intent)
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("subprocess must not run"))

    with pytest.raises(BenchExecutionAdmissionError, match="only for a running"):
        RunBoundBenchExecutor(store).execute(draft.id, BuildApp(target), inspected_target=target)

    store.transition(draft.id, RunState.CANCELLED)
    run = _running_run(store, target)
    other = InspectedBenchTarget("/srv/frappe/other", "harness.local", "task_tracker")
    with pytest.raises(BenchTargetBindingError, match="does not match"):
        RunBoundBenchExecutor(store).execute(run.id, BuildApp(other), inspected_target=other)


@pytest.mark.parametrize("operation_type", [SiteMigrate, NewAppScaffold])
def test_executor_defers_migration_and_interactive_scaffolding_before_receipt_or_subprocess(
    monkeypatch, store, target, operation_type
):
    run = _running_run(store, target)
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("subprocess must not run"))

    with pytest.raises(DeferredBenchOperation, match="later dedicated orchestration path"):
        RunBoundBenchExecutor(store).execute(run.id, operation_type(target), inspected_target=target)

    with store._transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM command_receipts").fetchone()[0] == 0


def test_timeout_finishes_a_failed_empty_receipt_without_diagnostic_output(monkeypatch, store, target):
    run = _running_run(store, target)

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(("bench", "build"), 1)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(BenchCommandTimedOut):
        RunBoundBenchExecutor(store, runner=BenchRunner(timeout_seconds=1)).execute(
            run.id, BuildApp(target), inspected_target=target
        )

    with store._transaction() as connection:
        row = connection.execute(
            "SELECT status, exit_code, stdout_bytes, stderr_bytes FROM command_receipts"
        ).fetchone()
    assert tuple(row) == ("failed", None, 0, 0)


def test_budgeted_output_limit_finishes_a_failed_empty_receipt(monkeypatch, store, target):
    run = _running_run(store, target)

    def oversized(*_args, **_kwargs):
        return subprocess.CompletedProcess(("bench", "build"), 0, b"x" * 32, b"")

    monkeypatch.setattr(subprocess, "run", oversized)
    with pytest.raises(BenchOutputTooLarge):
        RunBoundBenchExecutor(
            store,
            budget=OperationalBudget(max_log_bytes=8, max_artifact_bytes=16),
        ).execute(run.id, BuildApp(target), inspected_target=target)

    with store._transaction() as connection:
        row = connection.execute(
            "SELECT status, exit_code, stdout_bytes, stderr_bytes FROM command_receipts"
        ).fetchone()
    assert tuple(row) == ("failed", None, 0, 0)


def test_executor_loads_the_immutable_run_budget_when_no_runner_is_injected(monkeypatch, store, target):
    run = _running_run(store, target, budget=OperationalBudget(max_log_bytes=8, max_artifact_bytes=16))

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(("bench", "build"), 0, b"x" * 32, b""),
    )
    with pytest.raises(BenchOutputTooLarge):
        RunBoundBenchExecutor(store).execute(run.id, BuildApp(target), inspected_target=target)

    assert store.get_budget(run.id) == OperationalBudget(max_log_bytes=8, max_artifact_bytes=16)


def test_run_bound_bench_executor_rejects_ambiguous_runner_and_budget(store):
    with pytest.raises(ValueError, match="runner or budget"):
        RunBoundBenchExecutor(store, runner=BenchRunner(), budget=OperationalBudget())
