from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import subprocess

import pytest

from frappe_harness.bench_command_policy import InspectedBenchTarget
from frappe_harness.bench_runner import DockerBenchRunner, DockerContainerTarget, DockerSourceCheckpointError
from frappe_harness.compiler import compile_project
from frappe_harness.live_chain_providers import (
    CleanSourceCheckpointError,
    CleanSourceCheckpointProvider,
    GeneratedArtifactDeploymentError,
    GeneratedArtifactDeploymentProvider,
)
from frappe_harness.run_store import LockTarget
from frappe_harness.spec_io import load_project_spec


@pytest.fixture
def target() -> InspectedBenchTarget:
    return InspectedBenchTarget("/srv/frappe/harness", "harness.local", "task_tracker")


@pytest.fixture
def compilation():
    spec = load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")
    return compile_project(spec)


def _lock(target: InspectedBenchTarget) -> LockTarget:
    return LockTarget(target.bench_path, target.site_name, target.app_slug)


def test_deployer_verifies_bundle_and_writes_manifest_last(monkeypatch, target, compilation):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = GeneratedArtifactDeploymentProvider(
        target, DockerBenchRunner(DockerContainerTarget("frappe-1")),
    )
    result = provider.deploy(target=_lock(target), compilation=compilation)

    assert result.artifact_count == len(compilation.artifacts)
    assert result.total_bytes == sum(map(len, compilation.artifacts.values()))
    assert result.reference.startswith("docker-generated-artifacts:")
    assert len(calls) == len(compilation.artifacts)
    assert calls[-1][0][-3] == "task_tracker/harness/ownership-manifest.json"
    for argv, kwargs in calls:
        path, digest, app = argv[-3:]
        assert argv[:6] == (
            "docker", "exec", "-i", "--workdir", "/srv/frappe/harness", "frappe-1",
        )
        assert app == "task_tracker"
        assert kwargs["shell"] is False
        assert kwargs["input"] == compilation.artifacts[path]
        assert sha256(kwargs["input"]).hexdigest() == digest


def test_deployer_rejects_tampering_and_cross_target_before_process(monkeypatch, target, compilation):
    def forbidden_run(*_args, **_kwargs):
        raise AssertionError("invalid deployment must not start a process")

    monkeypatch.setattr(subprocess, "run", forbidden_run)
    provider = GeneratedArtifactDeploymentProvider(
        target, DockerBenchRunner(DockerContainerTarget("frappe-1")),
    )
    path = next(path for path in compilation.artifacts if not path.endswith("ownership-manifest.json"))
    tampered = replace(compilation, artifacts={**compilation.artifacts, path: b"tampered"})
    with pytest.raises(GeneratedArtifactDeploymentError, match="audit failed"):
        provider.deploy(target=_lock(target), compilation=tampered)
    with pytest.raises(GeneratedArtifactDeploymentError, match="does not match"):
        provider.deploy(
            target=LockTarget(target.bench_path, "other.local", target.app_slug),
            compilation=compilation,
        )


def test_deployer_fails_on_first_nonzero_write(monkeypatch, target, compilation):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 7, b"", b"not retained")

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = GeneratedArtifactDeploymentProvider(
        target, DockerBenchRunner(DockerContainerTarget("frappe-1")),
    )
    with pytest.raises(GeneratedArtifactDeploymentError, match="did not complete"):
        provider.deploy(target=_lock(target), compilation=compilation)


def test_clean_checkpoint_uses_only_exact_app_git_status_and_head(monkeypatch, target):
    calls = []
    head = b"a" * 40 + b"\n"

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        output = b"" if "status" in argv else head
        return subprocess.CompletedProcess(argv, 0, output, b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = CleanSourceCheckpointProvider(
        target, DockerBenchRunner(DockerContainerTarget("frappe-1")),
    )
    receipt = provider.create(
        target=_lock(target), spec_hash="b" * 64, plan_hash="c" * 64,
    )

    assert receipt.checkpoint == "a" * 40
    assert receipt.reference == "git:" + "a" * 40
    assert receipt.provider_id == "docker-git-clean"
    assert [call[0][-4:] for call in calls] == [
        ("git", "status", "--porcelain=v1", "--untracked-files=all"),
        ("git", "rev-parse", "--verify", "HEAD"),
    ]
    assert all(call[0][4] == "/srv/frappe/harness/apps/task_tracker" for call in calls)


def test_clean_checkpoint_rejects_dirty_source_and_bad_binding_before_head(monkeypatch, target):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b" M task_tracker/hooks.py\n", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = CleanSourceCheckpointProvider(
        target, DockerBenchRunner(DockerContainerTarget("frappe-1")),
    )
    with pytest.raises(DockerSourceCheckpointError, match="clean app repository"):
        provider.create(target=_lock(target), spec_hash="b" * 64, plan_hash="c" * 64)
    assert len(calls) == 1
    with pytest.raises(CleanSourceCheckpointError, match="plan_hash"):
        provider.create(target=_lock(target), spec_hash="b" * 64, plan_hash="invalid")
    assert len(calls) == 1
