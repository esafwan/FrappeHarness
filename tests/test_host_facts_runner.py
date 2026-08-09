from __future__ import annotations

import subprocess

import pytest

from frappe_harness.environment_preflight import DiskHealth, HealthCheck, ToolStatus
from frappe_harness.host_facts_runner import (
    HostFactsAcquisitionError,
    HostFactsRunner,
    HostSupplementalFacts,
)
from frappe_harness.operational_budget import OperationalBudget


BENCH_PATH = "/home/frappe/frappe-bench"


def _disk_usage(total, used, free):
    """Return a ``shutil.disk_usage``-shaped namedtuple factory."""

    from collections import namedtuple

    Usage = namedtuple("Usage", ["total", "used", "free"])
    return Usage(total=total, used=used, free=free)


def test_host_facts_runner_gathers_disk_and_tool_facts(monkeypatch):
    calls = []

    def process(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv == ("bench", "--version"):
            return subprocess.CompletedProcess(argv, 0, b"bench, version 5.22.3\n", b"")
        if argv == ("frappectl", "--version"):
            return subprocess.CompletedProcess(argv, 0, b"frappectl version 0.4.1\n", b"")
        return subprocess.CompletedProcess(argv, 1, b"", b"unexpected")

    monkeypatch.setattr("shutil.disk_usage", lambda path: _disk_usage(100, 40, 60))
    monkeypatch.setattr("os.access", lambda path, mode: mode == 0o2 and path == BENCH_PATH)

    facts = HostFactsRunner(process=process).inspect(BENCH_PATH)

    assert facts.disk == DiskHealth(free_bytes=60, writable=True)
    assert facts.tools == (
        ToolStatus(name="bench", available=True, version="5.22.3"),
        ToolStatus(name="frappectl", available=True, version="0.4.1"),
    )
    assert facts.health_checks == ()
    assert [argv for argv, _ in calls] == [("bench", "--version"), ("frappectl", "--version")]
    for argv, kwargs in calls:
        assert kwargs["shell"] is False
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is False


def test_host_facts_runner_reports_missing_tools(monkeypatch):
    def process(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr("shutil.disk_usage", lambda path: _disk_usage(100, 50, 50))
    monkeypatch.setattr("os.access", lambda path, mode: False)

    facts = HostFactsRunner(process=process).inspect(BENCH_PATH)

    assert facts.disk == DiskHealth(free_bytes=50, writable=False)
    assert facts.tools == (
        ToolStatus(name="bench", available=False, version=None),
        ToolStatus(name="frappectl", available=False, version=None),
    )


def test_host_facts_runner_reports_nonzero_version_exit(monkeypatch):
    def process(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, b"", b"not installed")

    monkeypatch.setattr("shutil.disk_usage", lambda path: _disk_usage(100, 10, 90))
    monkeypatch.setattr("os.access", lambda path, mode: True)

    facts = HostFactsRunner(process=process).inspect(BENCH_PATH)

    assert facts.disk.writable is True
    assert all(tool.available is False for tool in facts.tools)


def test_host_facts_runner_fail_closed_on_unparseable_version(monkeypatch):
    def process(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, b"unexpected output\n", b"")

    monkeypatch.setattr("shutil.disk_usage", lambda path: _disk_usage(100, 10, 90))
    monkeypatch.setattr("os.access", lambda path, mode: True)

    with pytest.raises(HostFactsAcquisitionError, match="parseable version"):
        HostFactsRunner(process=process).inspect(BENCH_PATH)


def test_host_facts_runner_fail_closed_on_invalid_bench_path():
    runner = HostFactsRunner()
    for bad in ["", "relative/path", "/", "/home//bench", "/home/bench/", "/home/../bench"]:
        with pytest.raises(HostFactsAcquisitionError):
            runner.inspect(bad)


def test_host_facts_runner_enforces_output_budget(monkeypatch):
    budget = OperationalBudget(
        command_timeout_seconds=10.0,
        max_log_bytes=5,
        max_artifact_bytes=20,
    )

    def process(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, b"1234567890", b"1234567890")

    monkeypatch.setattr("shutil.disk_usage", lambda path: _disk_usage(100, 10, 90))
    monkeypatch.setattr("os.access", lambda path, mode: True)

    with pytest.raises(HostFactsAcquisitionError, match="exceeds the evidence budget"):
        HostFactsRunner(budget=budget, process=process).inspect(BENCH_PATH)


def test_host_facts_runner_uses_budget_timeout(monkeypatch):
    budget = OperationalBudget(command_timeout_seconds=5.0)
    calls = []

    def process(argv, **kwargs):
        calls.append(kwargs["timeout"])
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr("shutil.disk_usage", lambda path: _disk_usage(100, 10, 90))
    monkeypatch.setattr("os.access", lambda path, mode: True)

    HostFactsRunner(timeout_seconds=30.0, budget=budget, process=process).inspect(BENCH_PATH)
    assert calls == [5.0, 5.0]


def test_host_supplemental_facts_validates_types():
    with pytest.raises(HostFactsAcquisitionError, match="disk facts"):
        HostSupplementalFacts(disk=None, tools=(), health_checks=())  # type: ignore[arg-type]
    with pytest.raises(HostFactsAcquisitionError, match="tool facts"):
        HostSupplementalFacts(disk=DiskHealth(1, True), tools=[], health_checks=())  # type: ignore[list-item]
    with pytest.raises(HostFactsAcquisitionError, match="health checks"):
        HostSupplementalFacts(disk=DiskHealth(1, True), tools=(), health_checks=[])  # type: ignore[list-item]


def test_host_facts_runner_composes_with_bench_facts_preflight(monkeypatch):
    """Host supplemental facts satisfy the typed inputs BenchFactsSnapshot.to_preflight requires."""

    from frappe_harness.bench_command_policy import InspectedBenchTarget
    from frappe_harness.bench_facts_runner import BenchFactsRunner

    outputs = [
        b"frappe 16.29.0 version-16 (abc)\n",
        b'{"dev.local":["frappe","huf","task_tracker"]}',
        b'{"dev.local":{"developer_mode":1,"db_type":"mariadb"}}',
    ]

    def bench_process(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, outputs.pop(0), b"")

    def host_process(argv, **kwargs):
        if argv == ("bench", "--version"):
            return subprocess.CompletedProcess(argv, 0, b"5.22.3\n", b"")
        if argv == ("frappectl", "--version"):
            raise FileNotFoundError("frappectl")
        return subprocess.CompletedProcess(argv, 1, b"", b"")

    monkeypatch.setattr("shutil.disk_usage", lambda path: _disk_usage(10_000_000_000, 1, 9_000_000_000))
    monkeypatch.setattr("os.access", lambda path, mode: True)

    target = InspectedBenchTarget(BENCH_PATH, "dev.local", "task_tracker")
    bench_facts = BenchFactsRunner(process=bench_process).inspect(target)
    host_facts = HostFactsRunner(process=host_process).inspect(target.bench_path)

    preflight = bench_facts.to_preflight(
        tools=host_facts.tools,
        disk=host_facts.disk,
        health_checks=(HealthCheck("database", True), HealthCheck("site", True)),
    )

    assert preflight.identity.bench == BENCH_PATH
    assert preflight.frappe.major == 16
    assert preflight.disk.free_bytes == 9_000_000_000
    assert {tool.name for tool in preflight.tools} == {"bench", "frappectl"}
    assert preflight.safety.production_designated is None
