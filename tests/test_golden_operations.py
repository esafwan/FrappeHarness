from __future__ import annotations

import subprocess

import pytest

from frappe_harness.bench_command_policy import InspectedBenchTarget
from frappe_harness.bench_runner import BenchRunner
from frappe_harness.golden_operations import GoldenOperationsError, RunBoundGoldenOperationsExecutor
from frappe_harness.run_store import LockTarget, RunIntent, RunState, RunStore


def _running(store: RunStore, target: InspectedBenchTarget):
    intent = RunIntent(LockTarget(target.bench_path, target.site_name, target.app_slug), "frappe-native", "a" * 64, "b" * 64)
    run = store.create_run(intent)
    store.transition(run.id, RunState.PENDING_APPROVAL)
    store.record_approval(run.id, approver="operator", approved=True, intent=intent)
    store.transition(run.id, RunState.APPROVED)
    return store.transition(run.id, RunState.RUNNING)


def test_run_bound_golden_operations_persist_typed_receipts_and_browser_evidence(tmp_path, monkeypatch):
    store = RunStore(tmp_path / "runs.sqlite3")
    target = InspectedBenchTarget("/bench", "dev.local", "task_tracker")
    run = _running(store, target)
    seen = []

    def process(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"ok", b"")

    monkeypatch.setattr(subprocess, "run", process)
    result = RunBoundGoldenOperationsExecutor(store, bench=None).execute(
        run.id, inspected_target=target, browser_probe=lambda: True
    )
    assert seen == [
        ("bench", "build", "--app", "task_tracker"),
        ("bench", "--site", "dev.local", "clear-cache"),
        ("bench", "--site", "dev.local", "run-tests", "--app", "task_tracker"),
    ]
    assert [receipt.status for receipt in result.command_receipts] == ["completed"] * 3
    assert result.evidence.kind == "golden_operations"
    assert result.browser_evidence.kind == "browser_acceptance"


def test_browser_failure_never_records_browser_evidence(tmp_path, monkeypatch):
    store = RunStore(tmp_path / "runs.sqlite3")
    target = InspectedBenchTarget("/bench", "dev.local", "task_tracker")
    run = _running(store, target)
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b"", b""))

    with pytest.raises(GoldenOperationsError, match="browser acceptance"):
        RunBoundGoldenOperationsExecutor(store).execute(run.id, inspected_target=target, browser_probe=lambda: False)
    with store._transaction() as connection:
        kinds = [row[0] for row in connection.execute("SELECT kind FROM lifecycle_evidence").fetchall()]
    assert kinds == ["golden_operations"]
