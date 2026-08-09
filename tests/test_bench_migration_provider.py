from __future__ import annotations

from pathlib import Path

import pytest

from frappe_harness.bench_command_policy import InspectedBenchTarget
from frappe_harness.bench_migration_provider import BenchMigrationProvider
from frappe_harness.bench_runner import (
    BenchExecutionResult,
    DockerBenchRunner,
    DockerContainerTarget,
    RedactedBenchReceipt,
)
from frappe_harness.operational_budget import OperationalBudget
from frappe_harness.compiler import compile_project
from frappe_harness.provider_plan import build_metadata_plan
from frappe_harness.run_store import LockTarget
from frappe_harness.spec_io import load_project_spec


class FakeRunner:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run(self, operation, *, inspected_target):
        self.calls.append((operation, inspected_target))
        return self.result


def _inputs():
    spec = load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")
    return build_metadata_plan(spec), compile_project(spec)


def test_bench_migration_provider_uses_only_typed_site_migrate():
    plan, compilation = _inputs()
    target = InspectedBenchTarget("/tmp/frappe-bench", "dev.local", "task_tracker")
    result = BenchExecutionResult(RedactedBenchReceipt(
        ("bench", "--site", "dev.local", "migrate"), 0, "", "", 0, "a" * 64, 2, "b" * 64,
    ))
    runner = FakeRunner(result)
    receipt = BenchMigrationProvider(target, runner=runner).apply(
        target=LockTarget("/tmp/frappe-bench", "dev.local", "task_tracker"),
        plan=plan, compilation=compilation,
    )

    assert receipt.exit_code == 0
    assert receipt.stdout_sha256 == "a" * 64
    assert type(runner.calls[0][0]).__name__ == "SiteMigrate"


def test_bench_migration_provider_rejects_cross_target_before_runner():
    plan, compilation = _inputs()
    target = InspectedBenchTarget("/tmp/frappe-bench", "dev.local", "task_tracker")
    runner = FakeRunner(None)
    with pytest.raises(ValueError, match="target"):
        BenchMigrationProvider(target, runner=runner).apply(
            target=LockTarget("/tmp/other-bench", "dev.local", "task_tracker"),
            plan=plan, compilation=compilation,
        )
    assert runner.calls == []


def test_container_migration_provider_rebinds_the_run_budget_without_losing_transport():
    target = InspectedBenchTarget("/workspace/frappe-bench", "dev.local", "task_tracker")
    container = DockerContainerTarget("frappe_devcontainer-frappe-1")
    provider = BenchMigrationProvider(target, container=container)
    budget = OperationalBudget(command_timeout_seconds=7, max_log_bytes=1234)

    bound = provider.bind_budget(budget)

    assert bound is not provider
    assert isinstance(bound.runner, DockerBenchRunner)
    assert bound.container == container
    assert bound.runner.container == container
    assert bound.runner.budget == budget


def test_container_migration_provider_rejects_ambiguous_runner_configuration():
    target = InspectedBenchTarget("/workspace/frappe-bench", "dev.local", "task_tracker")
    container = DockerContainerTarget("frappe-1")
    with pytest.raises(ValueError, match="runner or container"):
        BenchMigrationProvider(target, runner=FakeRunner(None), container=container)
