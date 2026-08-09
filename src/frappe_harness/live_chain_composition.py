"""Authoritative, side-effect-free composition of disposable chain inputs.

This module constructs the typed provider graph only after bundle references,
parent intent, and target-bound preflight facts have been verified.  It does
not execute a provider; the caller must invoke the run-bound executor
explicitly after this composition succeeds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .bench_command_policy import InspectedBenchTarget
from .bench_execution import RunBoundBenchExecutor
from .bench_migration_provider import BenchMigrationProvider
from .bench_runner import DockerBenchRunner, DockerContainerTarget
from .contracts import ProjectSpec
from .compiler import CompilationResult
from .compiler import canonical_json
from .frontend_deployment import package_frontend_dist
from hashlib import sha256
from pathlib import Path
from .environment_preflight import TypedEnvironmentFactsCollector
from .frappe_affected_checks import BrowserProbe, FrappeAffectedCheckConfig, FrappeAffectedCheckProvider
from .frappe_rest_provider import FrappeRestProvider
from .live_chain_providers import GeneratedArtifactDeploymentProvider, CleanSourceCheckpointProvider
from .live_modification_chain import ExplicitSuccessorApproval
from .migration_execution import MigrationProvider
from .modification_orchestration import AffectedCheckProvider
from .operator_bundle import OperatorInputBundle
from .operator_input_loaders import (
    load_confirmed_compilation,
    load_confirmed_plan,
    load_confirmed_preflight,
    load_confirmed_spec,
)
from .recovery_execution import RunBoundRecoveryExecutor, SourceCheckpointProvider
from .run_store import LockTarget, RunStore
from .schema_diff import SchemaDiffReport, classify_schema_diff
from .provider_plan import MetadataPlan


@dataclass(frozen=True)
class DisposableChainInputs:
    installed: ProjectSpec
    confirmed: ProjectSpec
    plan: MetadataPlan
    compilation: CompilationResult
    schema_diff: SchemaDiffReport
    preflight: object
    approval: ExplicitSuccessorApproval
    artifact_deployer: GeneratedArtifactDeploymentProvider
    recovery_executor: RunBoundRecoveryExecutor
    checkpoint_provider: SourceCheckpointProvider
    migration_provider: MigrationProvider
    affected_provider: AffectedCheckProvider


def compose_disposable_chain_inputs(
    bundle: OperatorInputBundle,
    *,
    store: RunStore,
    target: InspectedBenchTarget,
    container: DockerContainerTarget,
    rest: FrappeRestProvider,
    task_name: str,
    expected_record: Mapping[str, object],
    browser_probe: BrowserProbe,
    migration_provider: MigrationProvider | None = None,
) -> DisposableChainInputs:
    """Build the exact provider graph; reject anything not schema-v2/bound."""
    if not isinstance(bundle, OperatorInputBundle) or bundle.schema_version != 2 or bundle.successor is None:
        raise ValueError("disposable chain composition requires an approved schema-v2 bundle")
    if not isinstance(store, RunStore) or not isinstance(container, DockerContainerTarget):
        raise ValueError("composition requires a RunStore and DockerContainerTarget")
    expected = LockTarget(target.bench_path, target.site_name, target.app_slug)
    if bundle.target != expected:
        raise ValueError("operator bundle target does not match the inspected target")
    installed = load_confirmed_spec(bundle.spec)
    confirmed = load_confirmed_spec(bundle.successor.confirmed_spec)
    plan = load_confirmed_plan(bundle.successor.confirmed_plan, confirmed)
    compilation = load_confirmed_compilation(bundle.successor.confirmed_compilation, confirmed)
    # The compiler envelope is intentionally deterministic and excludes the
    # separately built Vite runtime. Add that runtime only at the live
    # deployment boundary, then rebuild the ownership manifest so the
    # artifact provider still audits one exact bundle.
    frontend = package_frontend_dist(target.app_slug, Path(__file__).resolve().parents[2] / "frontend" / "dist")
    artifacts = dict(compilation.artifacts)
    artifacts.update(frontend)
    manifest = dict(compilation.ownership_manifest)
    manifest["owned_files"] = [
        {"path": path, "sha256": sha256(content).hexdigest()}
        for path, content in sorted(artifacts.items())
        if not path.endswith("/ownership-manifest.json")
    ]
    manifest_path = f"{target.app_slug}/harness/ownership-manifest.json"
    artifacts[manifest_path] = canonical_json(manifest)
    compilation = CompilationResult(dict(sorted(artifacts.items())), manifest, compilation.spec_hash)
    facts = load_confirmed_preflight(bundle.preflight, expected)
    preflight = TypedEnvironmentFactsCollector(facts).collect(expected).preflight
    schema_diff = classify_schema_diff(installed, confirmed)
    runner = DockerBenchRunner(container)
    return DisposableChainInputs(
        installed=installed,
        confirmed=confirmed,
        plan=plan,
        compilation=compilation,
        schema_diff=schema_diff,
        preflight=preflight,
        approval=ExplicitSuccessorApproval(bundle.approver, bundle.approved, bundle.successor_intent, bundle.rationale),
        artifact_deployer=GeneratedArtifactDeploymentProvider(target, runner),
        recovery_executor=RunBoundRecoveryExecutor(store, bench=RunBoundBenchExecutor(store, runner=runner)),
        checkpoint_provider=CleanSourceCheckpointProvider(target, runner),
        migration_provider=migration_provider or BenchMigrationProvider(target, container=container),
        affected_provider=FrappeAffectedCheckProvider(
            rest,
            FrappeAffectedCheckConfig(
                bench=target.bench_path,
                site=target.site_name,
                app=target.app_slug,
                spec_hash=confirmed_hash(confirmed),
                plan_hash=plan.plan_hash,
                task_name=task_name,
                expected_record=expected_record,
                browser_probe=browser_probe,
            ),
            store=store,
        ),
    )


def confirmed_hash(spec: ProjectSpec) -> str:
    from .contracts import spec_hash
    return spec_hash(spec)
