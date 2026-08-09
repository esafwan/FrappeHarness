from __future__ import annotations

import subprocess

import pytest

from frappe_harness.bench_command_policy import InspectedBenchTarget
from frappe_harness.bench_facts_runner import (
    BenchFactsAcquisitionError,
    BenchFactsRunner,
    DockerBenchFactsRunner,
)
from frappe_harness.bench_runner import DockerContainerTarget
from frappe_harness.environment_preflight import DiskHealth, HealthCheck, ToolStatus


def _target():
    return InspectedBenchTarget("/bench", "dev.local", "task_tracker")


def test_bench_facts_runner_parses_only_fixed_version_and_apps_shapes():
    outputs = [b"frappe 16.29.0 version-16 (abc)\nhuf 1.0.0 master (def)\n", b'{"dev.local":["frappe","huf","task_tracker"]}', b'{"dev.local":{"developer_mode":1,"db_type":"mariadb","db_password":"must-not-escape","encryption_key":"must-not-escape"}}']

    def process(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, outputs.pop(0), b"")

    facts = BenchFactsRunner(process=process).inspect(_target())
    assert facts.identity.app == "task_tracker"
    assert facts.frappe.major == 16
    assert facts.installed_apps == ("frappe", "huf", "task_tracker")
    assert facts.developer_mode is True
    assert facts.database_type == "mariadb"
    assert facts.production_designated is None


@pytest.mark.parametrize("apps", [b'{"other.local":[]}', b'{"dev.local":["frappe","frappe"]}', b"not json"])
def test_bench_facts_runner_fails_closed_on_apps_drift(apps):
    outputs = [b"frappe 16.29.0 version-16 (abc)\n", apps, b'{"dev.local":{"developer_mode":1,"db_type":"mariadb"}}']

    def process(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, outputs.pop(0), b"")

    with pytest.raises(BenchFactsAcquisitionError):
        BenchFactsRunner(process=process).inspect(_target())


def test_bench_facts_runner_rejects_nonzero_output_without_retaining_body():
    def process(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 2, b"secret-ish", b"failure")

    with pytest.raises(BenchFactsAcquisitionError, match="non-zero"):
        BenchFactsRunner(process=process).inspect(_target())


def test_site_config_rejects_unknown_safety_values():
    outputs = [b"frappe 16.29.0 version-16 (abc)\n", b'{"dev.local":["frappe"]}', b'{"dev.local":{"developer_mode":null,"db_type":"mariadb"}}']

    def process(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, outputs.pop(0), b"")

    with pytest.raises(BenchFactsAcquisitionError, match="developer_mode"):
        BenchFactsRunner(process=process).inspect(_target())


@pytest.mark.parametrize("production,expected", [(False, False), (True, True)])
def test_site_config_retains_only_explicit_production_designation(production, expected):
    outputs = [
        b"frappe 16.29.0 version-16 (abc)\n",
        b'{"dev.local":["frappe"]}',
        (f'{{"dev.local":{{"developer_mode":1,"db_type":"mariadb",'
         f'"production_designated":{str(production).lower()}}}}}').encode(),
    ]

    def process(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, outputs.pop(0), b"")

    facts = BenchFactsRunner(process=process).inspect(_target())
    assert facts.production_designated is expected


def test_snapshot_requires_explicit_supplemental_facts_before_complete_preflight():
    outputs = [b"frappe 16.29.0 version-16 (abc)\n", b'{"dev.local":["frappe","huf","task_tracker"]}', b'{"dev.local":{"developer_mode":1,"db_type":"mariadb"}}']

    def process(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, outputs.pop(0), b"")

    facts = BenchFactsRunner(process=process).inspect(_target())
    preflight = facts.to_preflight(
        tools=(ToolStatus("bench", True),),
        disk=DiskHealth(10_000_000, True),
        health_checks=(HealthCheck("site", True),),
    )
    assert preflight.identity == facts.identity
    assert preflight.safety.production_designated is None


def test_snapshot_does_not_infer_missing_supplemental_facts():
    outputs = [b"frappe 16.29.0 version-16 (abc)\n", b'{"dev.local":["frappe","huf","task_tracker"]}', b'{"dev.local":{"developer_mode":1,"db_type":"mariadb"}}']

    def process(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, outputs.pop(0), b"")

    facts = BenchFactsRunner(process=process).inspect(_target())
    with pytest.raises(BenchFactsAcquisitionError, match="complete preflight"):
        facts.to_preflight(tools=None, disk=None, health_checks=None)


def test_docker_bench_facts_runner_uses_only_typed_read_only_transport():
    outputs = [
        b"frappe 16.29.0 version-16 (abc)\n",
        b'{"dev.local":["frappe","huf","task_tracker"]}',
        b'{"dev.local":{"developer_mode":1,"db_type":"mariadb"}}',
    ]
    calls = []

    def process(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, outputs.pop(0), b"")

    facts = DockerBenchFactsRunner(
        DockerContainerTarget("frappe-1"), process=process,
    ).inspect(_target())

    assert facts.frappe.major == 16
    assert len(calls) == 3
    for argv, kwargs in calls:
        assert argv[:5] == ("docker", "exec", "--workdir", "/bench", "frappe-1")
        assert argv[5] == "bench"
        assert kwargs["cwd"] is None
        assert kwargs["shell"] is False
    assert calls[0][0][5:] == ("bench", "version")


def test_docker_bench_facts_runner_binds_every_allowlisted_argv_to_inspected_target():
    outputs = [
        b"frappe 16.29.0 version-16 (abc)\n",
        b'{"dev.local":["frappe"]}',
        b'{"dev.local":{"developer_mode":1,"db_type":"mariadb"}}',
    ]
    calls = []

    def process(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, outputs.pop(0), b"")

    DockerBenchFactsRunner(DockerContainerTarget("frappe-1"), process=process).inspect(_target())

    assert [argv[5:] for argv, _ in calls] == [
        ("bench", "version"),
        ("bench", "--site", "dev.local", "list-apps", "--format", "json"),
        ("bench", "--site", "dev.local", "show-config", "-f", "json"),
    ]
    for argv, kwargs in calls:
        assert argv[3] == "/bench"
        assert argv[4] == "frappe-1"
        assert kwargs["cwd"] is None
        assert kwargs["shell"] is False


def test_docker_bench_facts_runner_rejects_untyped_container():
    with pytest.raises(BenchFactsAcquisitionError, match="container"):
        DockerBenchFactsRunner("frappe-1")
