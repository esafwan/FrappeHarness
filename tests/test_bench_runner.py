from __future__ import annotations

import hashlib
import subprocess

import pytest

from frappe_harness.bench_command_policy import InspectedBenchTarget, SiteMigrate
from frappe_harness.bench_runner import (
    BenchCommandTimedOut,
    BenchOutputTooLarge,
    BenchRunner,
    BenchToolUnavailable,
    DockerBenchRunner,
    DockerContainerPolicyError,
    DockerContainerTarget,
)
from frappe_harness.operational_budget import OperationalBudget


@pytest.fixture
def target() -> InspectedBenchTarget:
    return InspectedBenchTarget("/srv/frappe/harness", "harness.local", "task_tracker")


def test_runner_executes_only_policy_argv_in_inspected_bench_and_redacts_output(monkeypatch, target):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 2, b"ordinary output", b"sensitive diagnostic")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = BenchRunner(timeout_seconds=12).run(SiteMigrate(target), inspected_target=target)

    assert seen["argv"] == ("bench", "--site", "harness.local", "migrate")
    assert seen["kwargs"] == {
        "shell": False,
        "check": False,
        "cwd": "/srv/frappe/harness",
        "capture_output": True,
        "text": False,
        "timeout": 12.0,
    }
    assert result.receipt.exit_code == 2
    assert result.receipt.stdout_bytes == len(b"ordinary output")
    assert result.receipt.stderr_bytes == len(b"sensitive diagnostic")
    assert result.receipt.stdout_sha256 == hashlib.sha256(b"ordinary output").hexdigest()
    assert result.receipt.stderr_sha256 == hashlib.sha256(b"sensitive diagnostic").hexdigest()
    assert not hasattr(result.receipt, "stdout")
    assert not hasattr(result.receipt, "stderr")


def test_missing_bench_is_a_typed_unavailable_tool_error(monkeypatch, target):
    def fake_run(*_args, **_kwargs):
        raise FileNotFoundError("bench")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(BenchToolUnavailable, match="pinned Bench runtime"):
        BenchRunner().run(SiteMigrate(target), inspected_target=target)


def test_bench_timeout_is_a_typed_error(monkeypatch, target):
    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(("bench", "--site", "harness.local", "migrate"), 3)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(BenchCommandTimedOut, match="3-second execution limit"):
        BenchRunner(timeout_seconds=3).run(SiteMigrate(target), inspected_target=target)


def test_budget_caps_explicit_bench_timeout(monkeypatch, target):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["timeout"] = kwargs["timeout"]
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    BenchRunner(timeout_seconds=12, budget=OperationalBudget(command_timeout_seconds=5)).run(
        SiteMigrate(target), inspected_target=target,
    )
    assert seen["timeout"] == 5


def test_budget_rejects_oversized_output_before_receipt(monkeypatch, target):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, b"x" * 11, b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    budget = OperationalBudget(max_log_bytes=10, max_artifact_bytes=100)
    with pytest.raises(BenchOutputTooLarge):
        BenchRunner(budget=budget).run(SiteMigrate(target), inspected_target=target)


def test_budget_rejects_combined_output_above_artifact_limit(monkeypatch, target):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, b"x" * 6, b"y" * 6)

    monkeypatch.setattr(subprocess, "run", fake_run)
    budget = OperationalBudget(max_log_bytes=10, max_artifact_bytes=10)
    with pytest.raises(BenchOutputTooLarge):
        BenchRunner(budget=budget).run(SiteMigrate(target), inspected_target=target)


@pytest.mark.parametrize("timeout_seconds", [0, -1, float("inf"), float("nan"), True, "5"])
def test_timeout_must_be_a_positive_finite_number(timeout_seconds):
    with pytest.raises(ValueError, match="positive finite"):
        BenchRunner(timeout_seconds=timeout_seconds)


def test_docker_runner_wraps_only_policy_argv_and_redacts_output(monkeypatch, target):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, b"ordinary output", b"sensitive diagnostic")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = DockerBenchRunner(
        DockerContainerTarget("frappe_devcontainer-frappe-1"), timeout_seconds=12,
    ).run(SiteMigrate(target), inspected_target=target)

    assert seen["argv"] == (
        "docker", "exec", "--workdir", "/srv/frappe/harness",
        "frappe_devcontainer-frappe-1", "bench", "--site", "harness.local", "migrate",
    )
    assert seen["kwargs"] == {
        "shell": False,
        "check": False,
        "cwd": None,
        "capture_output": True,
        "text": False,
        "timeout": 12.0,
    }
    assert result.receipt.argv == seen["argv"]
    assert result.receipt.stdout_sha256 == hashlib.sha256(b"ordinary output").hexdigest()
    assert result.receipt.stderr_sha256 == hashlib.sha256(b"sensitive diagnostic").hexdigest()
    assert not hasattr(result.receipt, "stdout")
    assert not hasattr(result.receipt, "stderr")


@pytest.mark.parametrize(
    "name",
    ["", "-container", "container name", "container;touch", "container/name", "x" * 129, None],
)
def test_docker_container_name_is_allowlisted(name):
    with pytest.raises(DockerContainerPolicyError, match="allowlisted identifier"):
        DockerContainerTarget(name)


def test_docker_runner_rejects_raw_container_text_and_cross_target_before_subprocess(monkeypatch, target):
    with pytest.raises(DockerContainerPolicyError, match="DockerContainerTarget"):
        DockerBenchRunner("frappe-1")

    def forbidden_run(*_args, **_kwargs):
        raise AssertionError("subprocess must not run for a cross-target operation")

    monkeypatch.setattr(subprocess, "run", forbidden_run)
    other = InspectedBenchTarget("/srv/frappe/other", "other.local", "task_tracker")
    with pytest.raises(ValueError, match="does not match"):
        DockerBenchRunner(DockerContainerTarget("frappe-1")).run(
            SiteMigrate(target), inspected_target=other,
        )


def test_docker_runner_preserves_timeout_and_output_budget(monkeypatch, target):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["timeout"] = kwargs["timeout"]
        return subprocess.CompletedProcess(argv, 0, b"x" * 11, b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    budget = OperationalBudget(command_timeout_seconds=5, max_log_bytes=10, max_artifact_bytes=100)
    with pytest.raises(BenchOutputTooLarge):
        DockerBenchRunner(
            DockerContainerTarget("frappe-1"), timeout_seconds=12, budget=budget,
        ).run(SiteMigrate(target), inspected_target=target)
    assert seen["timeout"] == 5


def test_missing_docker_is_a_typed_unavailable_tool_error(monkeypatch, target):
    def fake_run(*_args, **_kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(BenchToolUnavailable, match="pinned Docker CLI"):
        DockerBenchRunner(DockerContainerTarget("frappe-1")).run(
            SiteMigrate(target), inspected_target=target,
        )
