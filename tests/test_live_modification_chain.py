from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import subprocess

import pytest

from frappe_harness.bench_command_policy import InspectedBenchTarget
from frappe_harness.bench_execution import RunBoundBenchExecutionResult
from frappe_harness.bench_runner import BenchExecutionResult, RedactedBenchReceipt
from frappe_harness.compiler import compile_project
from frappe_harness.contracts import spec_hash
from frappe_harness.environment_preflight import (
    DiskHealth,
    EnvironmentIdentity,
    EnvironmentPreflight,
    FrappeRuntime,
    HealthCheck,
    EnvironmentPreflightPolicy,
    SiteSafety,
    ToolStatus,
    evaluate_environment_preflight,
)
from frappe_harness.live_chain_providers import GeneratedArtifactDeploymentReceipt
from frappe_harness.live_modification_chain import (
    DISPOSABLE_APP,
    DISPOSABLE_BENCH,
    DISPOSABLE_CONTAINER,
    DISPOSABLE_SITE,
    DisposableLiveChainConfig,
    ExplicitSuccessorApproval,
    LiveModificationChainUnavailable,
    RunBoundLiveModificationChainExecutor,
    evaluate_live_chain_readiness,
    probe_disposable_target,
)
from frappe_harness.migration_execution import MigrationProviderReceipt
from frappe_harness.provider_plan import build_metadata_plan
from frappe_harness.recovery_execution import RunBoundRecoveryExecutor, SourceCheckpointReceipt
from frappe_harness.run_store import LockTarget, RunIntent, RunState, RunStore
from frappe_harness.runtime_lifecycle import RunLifecycleCoordinator
from frappe_harness.schema_diff import classify_schema_diff
from frappe_harness.spec_io import load_project_spec


class ProbeRunner:
    def __init__(self, exit_codes=(0, 0, 0)):
        self.exit_codes = iter(exit_codes)
        self.operations = []

    def run(self, operation, *, inspected_target):
        self.operations.append((type(operation).__name__, inspected_target))
        argv = ("bench", type(operation).__name__)
        receipt = RedactedBenchReceipt(argv, next(self.exit_codes), "", "", 0, "a" * 64, 0, "b" * 64)
        return BenchExecutionResult(receipt)


def _config(tmp_path, **changes):
    values = {
        "container": DISPOSABLE_CONTAINER,
        "bench_path": DISPOSABLE_BENCH,
        "site": DISPOSABLE_SITE,
        "app": DISPOSABLE_APP,
        "base_url": "http://127.0.0.1:8111",
        "store_path": tmp_path / "outside" / "evidence.sqlite3",
        "repository_root": tmp_path / "repository",
    }
    values.update(changes)
    return DisposableLiveChainConfig(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("container", "other-container"),
        ("bench_path", "/workspace/development/reference-bench"),
        ("site", "reference.local"),
        ("app", "other_app"),
    ),
)
def test_config_rejects_every_cross_target_dimension(tmp_path, field, value):
    with pytest.raises(ValueError, match="authorized disposable target"):
        _config(tmp_path, **{field: value})


@pytest.mark.parametrize(
    "base_url",
    (
        "https://example.com:8111",
        "http://user:secret@127.0.0.1:8111",
        "http://127.0.0.1:8111/path",
        "http://127.0.0.1",
    ),
)
def test_config_rejects_nonlocal_or_credentialed_origins(tmp_path, base_url):
    with pytest.raises(ValueError, match="base_url"):
        _config(tmp_path, base_url=base_url)


def test_config_requires_fresh_external_sqlite_store(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    with pytest.raises(ValueError, match="outside"):
        _config(
            tmp_path,
            repository_root=repository,
            store_path=repository / "evidence.sqlite3",
        )
    existing = tmp_path / "existing.sqlite3"
    existing.write_bytes(b"existing")
    with pytest.raises(ValueError, match="fresh"):
        _config(tmp_path, store_path=existing)


def test_probe_uses_only_three_fixed_read_only_operations(tmp_path):
    runner = ProbeRunner()
    assert probe_disposable_target(_config(tmp_path), runner)
    assert [name for name, _target in runner.operations] == [
        "InspectBenchVersion",
        "SiteListApps",
        "SiteShowConfig",
    ]
    assert all(target.bench_path == DISPOSABLE_BENCH for _name, target in runner.operations)


def test_probe_fails_closed_on_any_nonzero_receipt(tmp_path):
    runner = ProbeRunner((0, 1, 0))
    assert not probe_disposable_target(_config(tmp_path), runner)
    assert len(runner.operations) == 2


def test_readiness_names_missing_capabilities_and_never_claims_evidence(tmp_path):
    readiness = evaluate_live_chain_readiness(
        _config(tmp_path),
        ProbeRunner(),
        generated_artifact_deployer_available=False,
        clean_checkpoint_provider_available=False,
        full_chain_executor_available=False,
        credential_available=True,
    )
    assert readiness.blockers == (
        "typed_generated_artifact_deployer_missing",
        "clean_source_checkpoint_provider_missing",
        "full_chain_executor_composition_missing",
    )
    assert readiness.as_dict()["evidence_store_created"] is False
    assert readiness.as_dict()["gate_approval_recorded"] is False
    with pytest.raises(LiveModificationChainUnavailable, match="artifact_deployer"):
        readiness.require_ready()


def test_readiness_converts_probe_exception_to_diagnostic_blocker(tmp_path):
    class BrokenRunner:
        def run(self, *_args, **_kwargs):
            raise subprocess.SubprocessError("sensitive child detail")

    readiness = evaluate_live_chain_readiness(
        _config(tmp_path), BrokenRunner(),
        generated_artifact_deployer_available=True,
        clean_checkpoint_provider_available=True,
        full_chain_executor_available=True,
        credential_available=True,
    )
    assert readiness.blockers == ("exact_disposable_target_probe_failed",)
    assert "sensitive" not in str(readiness.as_dict())


class ChainBench:
    def __init__(self, store, events):
        self.store = store
        self.events = events

    def execute(self, run_id, operation, *, inspected_target):
        self.events.append("backup")
        command = self.store.start_command(
            run_id,
            command="bench",
            arguments={"operation": type(operation).__name__},
        )
        completed = self.store.complete_command(command.id, exit_code=0)
        return RunBoundBenchExecutionResult(None, completed)


class ChainCheckpoint:
    def __init__(self, events):
        self.events = events

    def create(self, *, target, spec_hash, plan_hash):
        self.events.append("checkpoint")
        return SourceCheckpointReceipt(
            "checkpoint:v1-source",
            "a" * 40,
            target,
            spec_hash,
            plan_hash,
        )


class ChainDeployer:
    def __init__(self, events, inspected_target=None):
        self.events = events
        self.inspected_target = inspected_target
        self.calls = 0

    def deploy(self, *, target, compilation):
        self.calls += 1
        self.events.append("deploy")
        receipt = RedactedBenchReceipt(
            ("docker", "generated-write"),
            0,
            "",
            "",
            0,
            "a" * 64,
            0,
            "b" * 64,
        )
        return GeneratedArtifactDeploymentReceipt(
            "deployment:v2",
            target,
            compilation.spec_hash,
            len(compilation.artifacts),
            sum(len(value) for value in compilation.artifacts.values()),
            (receipt,) * len(compilation.artifacts),
        )


class ChainMigration:
    provider_id = "frappe-native"

    def __init__(self, events):
        self.events = events
        self.calls = 0

    def apply(self, *, target, plan, compilation):
        self.calls += 1
        self.events.append("migrate")
        return MigrationProviderReceipt("migration:v2", 0)


class ChainAffected:
    def __init__(self, store, events):
        self.store = store
        self.events = events
        self.calls = 0

    def execute(self, run_id, check):
        self.calls += 1
        self.events.append(f"affected:{check}")
        run = self.store.get_run(run_id)
        result = {
            "passed": True,
            "target": {"bench": run.target.bench, "site": run.target.site, "app": run.target.app},
            "spec_hash": run.intent.spec_hash,
            "plan_hash": run.intent.plan_hash,
        }
        return result | ({"mutation_attempted": False} if check == "destructive_no_mutation" else {})


def _eligible_preflight(target):
    return EnvironmentPreflight(
        EnvironmentIdentity(target.bench, target.site, target.app),
        FrappeRuntime(16, "16.0"),
        SiteSafety(True, False),
        (),
        (ToolStatus("bench", True), ToolStatus("frappectl", True)),
        DiskHealth(2_000_000_000, True),
        (HealthCheck("database", True), HealthCheck("site", True)),
    )


def test_native_provider_preflight_does_not_require_optional_frappectl():
    target = LockTarget("/bench", "site.local", "task_tracker")
    preflight = EnvironmentPreflight(
        EnvironmentIdentity(target.bench, target.site, target.app),
        FrappeRuntime(16, "16.0"), SiteSafety(True, False), ("task_tracker",),
        (ToolStatus("bench", True),), DiskHealth(2_000_000_000, True),
        (HealthCheck("database", True), HealthCheck("site", True)),
    )
    result = evaluate_environment_preflight(
        preflight,
        policy=EnvironmentPreflightPolicy(required_tools=("bench",)),
    )
    assert result.eligible is True


def _chain_inputs(tmp_path):
    root = Path(__file__).parents[1]
    installed = load_project_spec(root / "fixtures" / "task_tracker.json")
    confirmed = load_project_spec(root / "fixtures" / "task_tracker_v2_reference_url.json")
    v1_plan = build_metadata_plan(installed)
    target = LockTarget("/bench", "dev.local", "task_tracker")
    store = RunStore(tmp_path / "chain.sqlite3")
    parent = store.create_run(RunIntent(target, v1_plan.provider, v1_plan.spec_hash, v1_plan.plan_hash))
    store.transition(parent.id, RunState.PENDING_APPROVAL)
    parent_approval = store.record_approval(
        parent.id,
        approver="operator",
        approved=True,
        intent=parent.intent,
    )
    store.transition(parent.id, RunState.APPROVED)
    parent = store.transition(parent.id, RunState.RUNNING)
    lifecycle = RunLifecycleCoordinator(store)
    lifecycle.record_preflight(parent.id, _eligible_preflight(target))
    lifecycle.record_compilation(parent.id, installed, compile_project(installed))
    lifecycle.record_plan_approval(parent.id, v1_plan, parent_approval.id)

    plan = build_metadata_plan(confirmed, provider=v1_plan.provider)
    compilation = compile_project(confirmed)
    expected_intent = RunIntent(target, plan.provider, plan.spec_hash, plan.plan_hash)
    return store, parent, installed, confirmed, plan, compilation, expected_intent


def test_live_executor_composes_explicit_successor_recovery_deploy_and_checks(tmp_path):
    store, parent, installed, confirmed, plan, compilation, expected_intent = _chain_inputs(tmp_path)
    events = []
    inspected = InspectedBenchTarget(parent.target.bench, parent.target.site, parent.target.app)
    deployer = ChainDeployer(events, inspected)
    migration = ChainMigration(events)
    affected = ChainAffected(store, events)

    result = RunBoundLiveModificationChainExecutor(store).execute(
        parent.id,
        installed=installed,
        confirmed=confirmed,
        reconfirmed_spec_hash=spec_hash(confirmed),
        plan=plan,
        compilation=compilation,
        schema_diff=classify_schema_diff(installed, confirmed),
        successor_approval=ExplicitSuccessorApproval(
            "operator",
            True,
            expected_intent,
            "explicitly approved exact V2 execution intent",
        ),
        preflight=_eligible_preflight(parent.target),
        inspected_target=inspected,
        artifact_deployer=deployer,
        recovery_executor=RunBoundRecoveryExecutor(store, bench=ChainBench(store, events)),
        checkpoint_provider=ChainCheckpoint(events),
        migration_provider=migration,
        affected_provider=affected,
    )

    assert store.get_run(parent.id).state is RunState.CANCELLED
    assert result.successor.state is RunState.RUNNING
    assert result.successor.intent == expected_intent
    assert result.successor.metadata["revision_of"] == parent.id
    assert result.approval.decision == "approved"
    assert result.recovery.backup.kind == "backup"
    assert result.deployment_evidence.kind == "generated_artifact_deployment"
    assert result.modification.migration_evidence.kind == "migration_receipt"
    assert result.modification.safe_modification.kind == "safe_modification"
    assert events == [
        "backup",
        "checkpoint",
        "deploy",
        "migrate",
        "affected:migration",
        "affected:preservation",
        "affected:browser",
        "affected:destructive_no_mutation",
    ]


def test_live_executor_rejects_approval_drift_before_any_provider_action(tmp_path):
    store, parent, installed, confirmed, plan, compilation, expected_intent = _chain_inputs(tmp_path)
    events = []
    wrong_intent = RunIntent(
        expected_intent.target,
        expected_intent.provider,
        expected_intent.spec_hash,
        "f" * 64,
    )
    inspected = InspectedBenchTarget(parent.target.bench, parent.target.site, parent.target.app)
    deployer = ChainDeployer(events, inspected)
    migration = ChainMigration(events)
    affected = ChainAffected(store, events)

    with pytest.raises(LiveModificationChainUnavailable, match="approval"):
        RunBoundLiveModificationChainExecutor(store).execute(
            parent.id,
            installed=installed,
            confirmed=confirmed,
            reconfirmed_spec_hash=spec_hash(confirmed),
            plan=plan,
            compilation=compilation,
            schema_diff=classify_schema_diff(installed, confirmed),
            successor_approval=ExplicitSuccessorApproval("operator", True, wrong_intent),
            preflight=_eligible_preflight(parent.target),
            inspected_target=inspected,
            artifact_deployer=deployer,
            recovery_executor=RunBoundRecoveryExecutor(store, bench=ChainBench(store, events)),
            checkpoint_provider=ChainCheckpoint(events),
            migration_provider=migration,
            affected_provider=affected,
        )

    assert events == []
    assert deployer.calls == migration.calls == affected.calls == 0
    assert store.get_run(parent.id).state is RunState.RUNNING


@pytest.mark.parametrize("binding", ["target", "store"])
def test_live_executor_rejects_unbound_provider_capabilities_before_successor(tmp_path, binding):
    store, parent, installed, confirmed, plan, compilation, expected_intent = _chain_inputs(tmp_path)
    events = []
    inspected = InspectedBenchTarget(parent.target.bench, parent.target.site, parent.target.app)
    deployer = ChainDeployer(events, inspected)
    affected_store = store
    expected_error = "artifact deployer does not bind"
    if binding == "target":
        deployer = ChainDeployer(
            events,
            InspectedBenchTarget(parent.target.bench, parent.target.site, "other_app"),
        )
    else:
        affected_store = RunStore(tmp_path / "foreign.sqlite3")
        expected_error = "affected-check provider is not bound"
    migration = ChainMigration(events)
    affected = ChainAffected(affected_store, events)

    with pytest.raises(LiveModificationChainUnavailable, match=expected_error):
        RunBoundLiveModificationChainExecutor(store).execute(
            parent.id,
            installed=installed,
            confirmed=confirmed,
            reconfirmed_spec_hash=spec_hash(confirmed),
            plan=plan,
            compilation=compilation,
            schema_diff=classify_schema_diff(installed, confirmed),
            successor_approval=ExplicitSuccessorApproval("operator", True, expected_intent),
            preflight=_eligible_preflight(parent.target),
            inspected_target=inspected,
            artifact_deployer=deployer,
            recovery_executor=RunBoundRecoveryExecutor(store, bench=ChainBench(store, events)),
            checkpoint_provider=ChainCheckpoint(events),
            migration_provider=migration,
            affected_provider=affected,
        )

    assert events == []
    assert store.get_run(parent.id).state is RunState.RUNNING


def test_live_executor_rejects_untrusted_deployment_provider_receipt(tmp_path):
    store, parent, _installed, confirmed, plan, compilation, _expected_intent = _chain_inputs(tmp_path)
    successor = replace(parent, intent=RunIntent(parent.target, plan.provider, plan.spec_hash, plan.plan_hash))
    receipts = tuple(
        RedactedBenchReceipt(
            ("docker", "generated-write"),
            0,
            "",
            "",
            0,
            "a" * 64,
            0,
            "b" * 64,
        )
        for _ in compilation.artifacts
    )
    valid = GeneratedArtifactDeploymentReceipt(
        "deployment:v2",
        successor.target,
        spec_hash(confirmed),
        len(compilation.artifacts),
        sum(len(value) for value in compilation.artifacts.values()),
        receipts,
    )
    with pytest.raises(LiveModificationChainUnavailable, match="artifact deployment receipt"):
        RunBoundLiveModificationChainExecutor._validate_deployment(
            successor,
            compilation,
            replace(valid, provider_id="untrusted-provider"),
        )
