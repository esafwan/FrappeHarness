"""Fail-closed readiness boundary for the disposable V1-to-V2 live chain.

This module does not manufacture migration evidence.  It proves that an
operator selected the one disposable target and that the closed Docker Bench
transport can inspect it.  The caller must also supply concrete generated-file
deployment and clean source-checkpoint capabilities before a durable chain may
start.  Until then, readiness fails before a :class:`RunStore` is created.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from .artifact_audit import audit_compilation
from .bench_command_policy import (
    InspectBenchVersion,
    InspectedBenchTarget,
    SiteListApps,
    SiteShowConfig,
)
from .compiler import CompilationResult
from .live_chain_providers import (
    GeneratedArtifactDeploymentProvider,
    GeneratedArtifactDeploymentReceipt,
)
from .migration_execution import MigrationProvider
from .modification_orchestration import (
    AffectedCheckProvider,
    ModificationAssessment,
    RunBoundModificationCoordinator,
    RunBoundSuccessorModificationExecutor,
    SuccessorModificationResult,
)
from .provider_plan import MetadataPlan
from .recovery_execution import (
    RecoveryEvidenceError,
    RecoveryEvidenceResult,
    RunBoundRecoveryExecutor,
    SourceCheckpointProvider,
)
from .run_store import LockTarget, RunRecord, RunState, RunStore
from .schema_diff import SchemaDiffReport
from .contracts import ProjectSpec
from .bench_runner import RedactedBenchReceipt
from .compiler import CompilationResult
from .contracts import ProjectSpec, spec_hash
from .environment_preflight import EnvironmentPreflight, EnvironmentPreflightPolicy, evaluate_environment_preflight
from .live_chain_providers import GeneratedArtifactDeploymentReceipt
from .migration_execution import MigrationProvider
from .modification_orchestration import (
    AffectedCheckProvider,
    RunBoundModificationCoordinator,
    RunBoundModificationResult,
    RunBoundSuccessorModificationExecutor,
    SuccessorModificationResult,
)
from .provider_plan import MetadataPlan
from .recovery_execution import (
    RecoveryEvidenceResult,
    RunBoundRecoveryExecutor,
    SourceCheckpointProvider,
)
from .run_store import ApprovalRecord, LifecycleEvidenceRecord, LockTarget, RunIntent, RunRecord, RunState, RunStore
from .runtime_lifecycle import RunLifecycleCoordinator
from .schema_diff import SchemaDiffReport, classify_schema_diff


DISPOSABLE_CONTAINER = "frappe_docker_devcontainer-frappe-1"
DISPOSABLE_BENCH = "/workspace/development/frappe-harness-live-20260804"
DISPOSABLE_SITE = "frappe-harness-live-20260804.local"
DISPOSABLE_APP = "task_tracker"


class LiveModificationChainUnavailable(RuntimeError):
    """The exact disposable live chain is not fully executable yet."""


class PostBackupRecoveryFailure(RuntimeError):
    """A post-backup step failed; no automatic restore was attempted.

    The successor run is terminalized as failed and durable failure evidence
    (backup plus failure phase/reason) is retained for operator reconciliation.
    """


class TypedBenchProbe(Protocol):
    def run(self, operation: object, *, inspected_target: InspectedBenchTarget) -> object: ...


class GeneratedArtifactDeployer(Protocol):
    """Closed capability for deploying an already-audited compilation."""

    def deploy(
        self, *, target: LockTarget, compilation: CompilationResult,
    ) -> GeneratedArtifactDeploymentReceipt: ...


@dataclass(frozen=True)
class ExplicitSuccessorApproval:
    """Operator decision supplied before a successor UUID exists.

    The exact successor intent is deterministic before the revision run is
    created, while its UUID is not.  Carrying the intent here lets the executor
    reject a denial or drift before it cancels the parent or invokes a provider.
    This is execution approval only; it never represents a milestone gate.
    """

    approver: str
    approved: bool
    intent: RunIntent
    rationale: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.approver, str) or not self.approver.strip():
            raise ValueError("successor approver must be non-blank")
        if not isinstance(self.approved, bool):
            raise ValueError("successor approval decision must be boolean")
        if not isinstance(self.intent, RunIntent):
            raise ValueError("successor approval must bind a RunIntent")
        if self.rationale is not None and (
            not isinstance(self.rationale, str) or not self.rationale.strip()
        ):
            raise ValueError("successor approval rationale must be non-blank when supplied")


@dataclass(frozen=True)
class RunBoundLiveModificationChainResult:
    """Durable identities returned only after the entire V1-to-V2 chain passes."""

    parent_modification: RunBoundModificationResult
    successor: RunRecord
    approval: ApprovalRecord
    recovery: RecoveryEvidenceResult
    deployment: GeneratedArtifactDeploymentReceipt
    deployment_evidence: LifecycleEvidenceRecord
    modification: SuccessorModificationResult


class RunBoundLiveModificationChainExecutor:
    """Compose the typed, durable V1-to-V2 mutation path for one run.

    All intent, target, hash, schema-diff, and explicit-approval inputs are
    checked before the parent is superseded.  Recovery is captured before
    generated files are written so a clean V1 source checkpoint can actually
    be proven.  No milestone-gate approval is created or inferred here.
    """

    def __init__(self, store: RunStore) -> None:
        if not isinstance(store, RunStore):
            raise ValueError("store must be a RunStore")
        self.store = store

    def execute(
        self,
        parent_run_id: str,
        *,
        installed: ProjectSpec,
        confirmed: ProjectSpec,
        reconfirmed_spec_hash: str,
        plan: MetadataPlan,
        compilation: CompilationResult,
        schema_diff: SchemaDiffReport,
        successor_approval: ExplicitSuccessorApproval,
        preflight: EnvironmentPreflight,
        inspected_target: InspectedBenchTarget,
        artifact_deployer: GeneratedArtifactDeployer,
        recovery_executor: RunBoundRecoveryExecutor,
        checkpoint_provider: SourceCheckpointProvider,
        migration_provider: MigrationProvider,
        affected_provider: AffectedCheckProvider,
    ) -> RunBoundLiveModificationChainResult:
        parent = self.store.get_run(parent_run_id)
        expected_intent = RunIntent(parent.target, plan.provider, plan.spec_hash, plan.plan_hash)
        self._validate_static_inputs(
            parent=parent,
            installed=installed,
            confirmed=confirmed,
            reconfirmed_spec_hash=reconfirmed_spec_hash,
            plan=plan,
            compilation=compilation,
            schema_diff=schema_diff,
            successor_approval=successor_approval,
            preflight=preflight,
            inspected_target=inspected_target,
            expected_intent=expected_intent,
            artifact_deployer=artifact_deployer,
            recovery_executor=recovery_executor,
            migration_provider=migration_provider,
            affected_provider=affected_provider,
        )

        coordinator = RunBoundModificationCoordinator(self.store)
        parent_modification = coordinator.assess(
            parent_run_id,
            installed,
            confirmed,
            reconfirmed_spec_hash=reconfirmed_spec_hash,
        )
        if not parent_modification.assessment.allowed or not parent_modification.assessment.reconfirmed:
            raise LiveModificationChainUnavailable("exact V2 reconfirmation did not admit the modification")

        successor = coordinator.create_revision_run(parent_run_id, parent_modification, plan=plan)
        if (
            successor.intent != expected_intent
            or successor.metadata.get("revision_of") != parent_run_id
            or successor.metadata.get("assessment_evidence_id") != parent_modification.evidence_id
        ):
            raise LiveModificationChainUnavailable("successor ancestry or immutable intent drifted")

        self.store.transition(successor.id, RunState.PENDING_APPROVAL)
        approval = self.store.record_approval(
            successor.id,
            approver=successor_approval.approver.strip(),
            approved=True,
            intent=successor_approval.intent,
            rationale=successor_approval.rationale,
        )
        self.store.transition(successor.id, RunState.APPROVED)
        successor = self.store.transition(successor.id, RunState.RUNNING)
        persisted_approval = self.store.get_approval(approval.id)
        if (
            persisted_approval.run_id != successor.id
            or persisted_approval.decision != "approved"
            or persisted_approval.intent != successor.intent
        ):
            raise LiveModificationChainUnavailable("successor approval did not persist for the exact intent")

        lifecycle = RunLifecycleCoordinator(self.store)
        lifecycle.record_preflight(
            successor.id,
            preflight,
            policy=EnvironmentPreflightPolicy(
                required_tools=("bench",)
                if plan.provider == "frappe-native"
                else ("bench", "frappectl"),
            ),
        )
        lifecycle.record_compilation(successor.id, confirmed, compilation)
        lifecycle.record_plan_approval(successor.id, plan, persisted_approval.id)

        # The checkpoint must describe the clean source that can restore the
        # target, so capture recovery before the deployer changes generated files.
        try:
            recovery = recovery_executor.execute(
                successor.id,
                inspected_target=inspected_target,
                checkpoint_provider=checkpoint_provider,
            )
        except RecoveryEvidenceError as error:
            if not error.after_backup:
                raise
            # Recovery failed after a successful backup. The recovery executor
            # already recorded durable failure evidence bound to the backup;
            # terminalize the successor and surface a typed failure without
            # attempting an automatic restore.
            self._terminalize_post_backup_failure(successor.id, error)
            raise PostBackupRecoveryFailure("recovery failed after backup") from error

        phase = "deployment"
        try:
            deployment = artifact_deployer.deploy(target=successor.target, compilation=compilation)
            self._validate_deployment(successor, compilation, deployment)
            deployment_evidence = self.store.record_lifecycle_evidence(
                successor.id,
                "generated_artifact_deployment",
                {
                    "provider_id": deployment.provider_id,
                    "reference": deployment.reference,
                    "target": {
                        "bench": successor.target.bench,
                        "site": successor.target.site,
                        "app": successor.target.app,
                    },
                    "spec_hash": deployment.spec_hash,
                    "artifact_count": deployment.artifact_count,
                    "total_bytes": deployment.total_bytes,
                },
            )
            phase = "modification"
            parent_assessment_ref = self.store.get_lifecycle_evidence(parent_modification.evidence_id).reference
            modification = RunBoundSuccessorModificationExecutor(self.store).execute(
                successor.id,
                parent_assessment_ref=parent_assessment_ref,
                installed=installed,
                confirmed=confirmed,
                plan=plan,
                compilation=compilation,
                schema_diff=schema_diff,
                approval_id=persisted_approval.id,
                backup_evidence_id=recovery.backup.id,
                checkpoint_evidence_id=recovery.checkpoint.id,
                migration_provider=migration_provider,
                affected_provider=affected_provider,
            )
        except Exception as error:
            self._record_post_backup_failure(
                successor.id,
                backup_evidence_id=recovery.backup.id,
                checkpoint_evidence_id=recovery.checkpoint.id,
                phase=phase,
                reason=f"{phase}_step_failed",
            )
            self._terminalize_post_backup_failure(successor.id, error)
            raise PostBackupRecoveryFailure(f"{phase} failed after backup") from error

        return RunBoundLiveModificationChainResult(
            parent_modification,
            successor,
            persisted_approval,
            recovery,
            deployment,
            deployment_evidence,
            modification,
        )

    def _validate_static_inputs(
        self,
        *,
        parent: RunRecord,
        installed: ProjectSpec,
        confirmed: ProjectSpec,
        reconfirmed_spec_hash: str,
        plan: MetadataPlan,
        compilation: CompilationResult,
        schema_diff: SchemaDiffReport,
        successor_approval: ExplicitSuccessorApproval,
        preflight: EnvironmentPreflight,
        inspected_target: InspectedBenchTarget,
        expected_intent: RunIntent,
        artifact_deployer: GeneratedArtifactDeployer,
        recovery_executor: RunBoundRecoveryExecutor,
        migration_provider: MigrationProvider,
        affected_provider: AffectedCheckProvider,
    ) -> None:
        if parent.state is not RunState.RUNNING:
            raise LiveModificationChainUnavailable("parent must be a running V1 run")
        confirmed_hash = spec_hash(confirmed)
        if spec_hash(installed) != parent.intent.spec_hash:
            raise LiveModificationChainUnavailable("installed specification does not bind the parent")
        if reconfirmed_spec_hash != confirmed_hash:
            raise LiveModificationChainUnavailable("confirmed V2 specification was not exactly reconfirmed")
        if (
            plan.provider != parent.intent.provider
            or plan.spec_hash != confirmed_hash
            or compilation.spec_hash != confirmed_hash
            or expected_intent == parent.intent
        ):
            raise LiveModificationChainUnavailable("V2 plan or compilation does not bind the successor intent")
        if schema_diff != classify_schema_diff(installed, confirmed):
            raise LiveModificationChainUnavailable("schema diff does not exactly describe V1 to V2")
        if not audit_compilation(compilation).valid:
            raise LiveModificationChainUnavailable("V2 compilation failed artifact audit")
        if not successor_approval.approved or successor_approval.intent != expected_intent:
            raise LiveModificationChainUnavailable("explicit successor approval is missing or intent-mismatched")
        target = InspectedBenchTarget(parent.target.bench, parent.target.site, parent.target.app)
        if inspected_target != target:
            raise LiveModificationChainUnavailable("inspected target does not bind the parent")
        if getattr(artifact_deployer, "inspected_target", None) != inspected_target:
            raise LiveModificationChainUnavailable("artifact deployer does not bind the inspected target")
        if getattr(affected_provider, "store", None) is not self.store:
            raise LiveModificationChainUnavailable("affected-check provider is not bound to this run store")
        identity = preflight.identity
        if (identity.bench, identity.site, identity.app) != (
            parent.target.bench,
            parent.target.site,
            parent.target.app,
        ):
            raise LiveModificationChainUnavailable("preflight identity does not bind the parent")
        # The native provider uses the closed Bench/Docker and REST seams; a
        # missing optional frappectl binary must not block that provider. The
        # frappectl requirement remains enforced for provider plans that name
        # it explicitly.
        policy = EnvironmentPreflightPolicy(
            required_tools=("bench",) if plan.provider == "frappe-native" else ("bench", "frappectl"),
        )
        eligibility = evaluate_environment_preflight(preflight, intent=expected_intent, policy=policy)
        if not eligibility.eligible:
            raise LiveModificationChainUnavailable(
                "successor preflight is ineligible: " + ",".join(eligibility.failure_codes)
            )
        if not isinstance(recovery_executor, RunBoundRecoveryExecutor) or recovery_executor.store is not self.store:
            raise LiveModificationChainUnavailable("recovery executor is not bound to this run store")
        if getattr(migration_provider, "provider_id", None) != plan.provider:
            raise LiveModificationChainUnavailable("migration provider does not bind the successor provider")

    @staticmethod
    def _validate_deployment(
        successor: RunRecord,
        compilation: CompilationResult,
        deployment: GeneratedArtifactDeploymentReceipt,
    ) -> None:
        if not isinstance(deployment, GeneratedArtifactDeploymentReceipt):
            raise LiveModificationChainUnavailable("artifact deployer returned an unsupported receipt")
        if (
            deployment.target != successor.target
            or deployment.spec_hash != successor.intent.spec_hash
            or deployment.artifact_count != len(compilation.artifacts)
            or deployment.total_bytes != sum(len(value) for value in compilation.artifacts.values())
            or not isinstance(deployment.reference, str)
            or not deployment.reference.strip()
            or not isinstance(deployment.provider_id, str)
            or deployment.provider_id != "docker-generated-artifacts"
            or not isinstance(deployment.write_receipts, tuple)
            or len(deployment.write_receipts) != deployment.artifact_count
            or any(
                not isinstance(receipt, RedactedBenchReceipt) or receipt.exit_code != 0
                for receipt in deployment.write_receipts
            )
        ):
            raise LiveModificationChainUnavailable("artifact deployment receipt does not bind the successor")

    def _record_post_backup_failure(
        self,
        run_id: str,
        *,
        backup_evidence_id: str,
        checkpoint_evidence_id: str,
        phase: str,
        reason: str,
    ) -> None:
        """Persist durable evidence that a post-backup step failed.

        The record explicitly attests ``auto_restore_attempted=false`` so the
        policy remains fail-closed: an authorized operator must reconcile.
        """
        self.store.record_lifecycle_evidence(run_id, "recovery_failure", {
            "recovery_receipt": "v1",
            "backup_evidence_id": backup_evidence_id,
            "checkpoint_evidence_id": checkpoint_evidence_id,
            "phase": phase,
            "reason": reason,
            "auto_restore_attempted": False,
        })

    def _terminalize_post_backup_failure(self, run_id: str, error: Exception) -> None:
        """Move the successor to a terminal failed state if still running."""
        try:
            self.store.transition(run_id, RunState.FAILED)
        except Exception:
            pass


@dataclass(frozen=True)
class DisposableLiveChainConfig:
    """Operator inputs for the one authorized disposable target."""

    container: str
    bench_path: str
    site: str
    app: str
    base_url: str
    store_path: Path
    repository_root: Path
    allow_existing_store: bool = False

    def __post_init__(self) -> None:
        exact = (
            ("container", self.container, DISPOSABLE_CONTAINER),
            ("bench_path", self.bench_path, DISPOSABLE_BENCH),
            ("site", self.site, DISPOSABLE_SITE),
            ("app", self.app, DISPOSABLE_APP),
        )
        for label, value, expected in exact:
            if value != expected:
                raise ValueError(f"{label} is not the authorized disposable target")
        _validate_local_origin(self.base_url)
        store = self.store_path
        if not isinstance(store, Path) or not store.is_absolute() or store.suffix != ".sqlite3":
            raise ValueError("store_path must be an absolute .sqlite3 path")
        repository = self.repository_root.resolve()
        resolved_store = store.resolve(strict=False)
        if resolved_store == repository or repository in resolved_store.parents:
            raise ValueError("live evidence store must be outside the repository")
        if store.is_symlink():
            raise ValueError("live evidence store must not be a symlink")
        if store.exists() and not self.allow_existing_store:
            raise ValueError("live evidence store must be a fresh non-symlink path")
        if store.exists() and not store.is_file():
            raise ValueError("existing live evidence store must be a regular file")

    @property
    def inspected_target(self) -> InspectedBenchTarget:
        return InspectedBenchTarget(self.bench_path, self.site, self.app)


@dataclass(frozen=True)
class LiveChainReadiness:
    """Normalized readiness result without child output or credentials."""

    target_probe_passed: bool
    generated_artifact_deployer_available: bool
    clean_checkpoint_provider_available: bool
    full_chain_executor_available: bool
    credential_available: bool

    @property
    def blockers(self) -> tuple[str, ...]:
        failures: list[str] = []
        if not self.target_probe_passed:
            failures.append("exact_disposable_target_probe_failed")
        if not self.generated_artifact_deployer_available:
            failures.append("typed_generated_artifact_deployer_missing")
        if not self.clean_checkpoint_provider_available:
            failures.append("clean_source_checkpoint_provider_missing")
        if not self.full_chain_executor_available:
            failures.append("full_chain_executor_composition_missing")
        if not self.credential_available:
            failures.append("process_local_administrator_credential_missing")
        return tuple(failures)

    @property
    def ready(self) -> bool:
        return not self.blockers

    def require_ready(self) -> None:
        if self.blockers:
            raise LiveModificationChainUnavailable(",".join(self.blockers))

    def as_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "target_probe_passed": self.target_probe_passed,
            "generated_artifact_deployer_available": self.generated_artifact_deployer_available,
            "clean_checkpoint_provider_available": self.clean_checkpoint_provider_available,
            "full_chain_executor_available": self.full_chain_executor_available,
            "credential_available": self.credential_available,
            "blockers": list(self.blockers),
            "evidence_store_created": False,
            "gate_approval_recorded": False,
        }


def probe_disposable_target(config: DisposableLiveChainConfig, runner: TypedBenchProbe) -> bool:
    """Run three fixed, read-only Bench probes through the closed transport."""

    target = config.inspected_target
    for operation in (InspectBenchVersion(target), SiteListApps(target), SiteShowConfig(target)):
        result = runner.run(operation, inspected_target=target)
        receipt = getattr(result, "receipt", None)
        if receipt is None or getattr(receipt, "exit_code", None) != 0:
            return False
    return True


def evaluate_live_chain_readiness(
    config: DisposableLiveChainConfig,
    runner: TypedBenchProbe,
    *,
    generated_artifact_deployer_available: bool,
    clean_checkpoint_provider_available: bool,
    full_chain_executor_available: bool,
    credential_available: bool,
) -> LiveChainReadiness:
    """Evaluate every prerequisite without opening the requested ledger."""

    try:
        target_passed = probe_disposable_target(config, runner)
    except Exception:
        target_passed = False
    return LiveChainReadiness(
        target_passed,
        generated_artifact_deployer_available is True,
        clean_checkpoint_provider_available is True,
        full_chain_executor_available is True,
        credential_available is True,
    )


def _validate_local_origin(value: object) -> None:
    if not isinstance(value, str) or value != value.strip():
        raise ValueError("base_url must be a local HTTP origin")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.port is None
        or parsed.hostname is None
    ):
        raise ValueError("base_url must be a local HTTP origin with an explicit port")
    host = parsed.hostname
    if host == "localhost":
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError("base_url host must be loopback or a private address") from error
    if not (address.is_loopback or address.is_private):
        raise ValueError("base_url host must be loopback or a private address")
