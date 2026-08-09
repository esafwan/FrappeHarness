"""Local, evidence-bound progression for the non-mutating §17 runtime gates."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from typing import Any, Mapping

from .artifact_audit import audit_compilation
from .compiler import CompilationResult
from .contracts import ProjectSpec, spec_hash, validate_project_spec
from .environment_preflight import CredentialProfile, EnvironmentPreflight, EnvironmentPreflightPolicy, evaluate_environment_preflight
from .operational_budget import OperationalBudget
from .lifecycle_contract import AttemptOutcome, LifecycleState, ResumeDecision, RetryPolicy, resume_decision as decide_resume
from .migration_gate import MigrationEvidence, MigrationGateInput, evaluate_pre_migration_gate
from .provider_plan import MetadataPlan
from .proposal_orchestration import NaturalLanguageProposalResult
from .run_store import IntentMismatch, LifecycleEvidenceRecord, LockTarget, PersistedLifecycleSnapshot, RunState, RunStore
from .schema_diff import SchemaDiffReport


class RuntimeLifecycleError(RuntimeError):
    """A runtime gate cannot attach trusted evidence to the current run."""


@dataclass(frozen=True)
class BusinessRunReceipt:
    """Typed, secret-free business receipt for one completed harness run."""

    run_id: str
    spec_hash: str
    plan_hash: str
    backend_report_ref: str
    frontend_scenario_count: int
    evidence_refs: tuple[str, ...] = ()
    deferred_requirements: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, value in (("run_id", self.run_id), ("spec_hash", self.spec_hash), ("plan_hash", self.plan_hash), ("backend_report_ref", self.backend_report_ref)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"business receipt {name} must be non-blank")
        for name, value in (("spec_hash", self.spec_hash), ("plan_hash", self.plan_hash)):
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"business receipt {name} must be a SHA-256 digest")
        if isinstance(self.frontend_scenario_count, bool) or not isinstance(self.frontend_scenario_count, int) or self.frontend_scenario_count < 1:
            raise ValueError("business receipt frontend_scenario_count must be positive")
        for name, values in (("evidence_refs", self.evidence_refs), ("deferred_requirements", self.deferred_requirements)):
            if not isinstance(values, tuple) or any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"business receipt {name} must be a tuple of non-blank strings")

    def as_mapping(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "spec_hash": self.spec_hash,
            "plan_hash": self.plan_hash,
            "backend_report_ref": self.backend_report_ref,
            "frontend_scenario_count": self.frontend_scenario_count,
            "evidence_refs": list(self.evidence_refs),
            "deferred_requirements": list(self.deferred_requirements),
        }


@dataclass(frozen=True)
class MigrationReconciliationReceipt:
    """External inspection fact required before a possibly-partial retry."""

    reference: str
    target: LockTarget
    spec_hash: str
    plan_hash: str
    outcome: str = "inspected"

    def __post_init__(self) -> None:
        if not isinstance(self.reference, str) or not self.reference.strip():
            raise ValueError("reconciliation reference must be non-blank")
        if not isinstance(self.target, LockTarget):
            raise ValueError("reconciliation target must be a LockTarget")
        if len(self.spec_hash) != 64 or any(char not in "0123456789abcdef" for char in self.spec_hash):
            raise ValueError("reconciliation spec_hash must be a SHA-256 digest")
        if len(self.plan_hash) != 64 or any(char not in "0123456789abcdef" for char in self.plan_hash):
            raise ValueError("reconciliation plan_hash must be a SHA-256 digest")
        if self.outcome not in {"inspected", "confirmed_not_applied", "confirmed_applied"}:
            raise ValueError("reconciliation outcome is unsupported")


@dataclass(frozen=True)
class MigrationReconciliationResult:
    receipt: MigrationReconciliationReceipt
    evidence: LifecycleEvidenceRecord
    snapshot: PersistedLifecycleSnapshot


class RunLifecycleCoordinator:
    """Advance only evidence-backed lifecycle gates; never execute a provider."""

    def __init__(self, store: RunStore) -> None:
        self.store = store

    def record_preflight(
        self, run_id: str, preflight: EnvironmentPreflight, *, policy: EnvironmentPreflightPolicy = EnvironmentPreflightPolicy(),
    ) -> PersistedLifecycleSnapshot:
        run = self.store.get_run(run_id)
        eligibility = evaluate_environment_preflight(preflight, policy=policy, intent=run.intent)
        if not eligibility.eligible:
            raise RuntimeLifecycleError("environment preflight is ineligible: " + ",".join(eligibility.failure_codes))
        evidence = self.store.record_lifecycle_evidence(run_id, "environment_preflight", {
            "preflight": _plain(preflight), "policy": _plain(policy), "eligibility": _plain(eligibility),
        })
        return self.store.advance_lifecycle_state(run_id, LifecycleState.BENCH_INSPECTED, (evidence.id,))

    def record_credential_profile(
        self, run_id: str, profile: CredentialProfile, *, budget: OperationalBudget | None = None,
    ) -> LifecycleEvidenceRecord:
        """Persist a secret-free profile lease bound to the run's exact target.

        The provider receives the secret through its short-lived process
        environment or session; the ledger stores only the profile identity
        and retention metadata.  A profile may never be attached to another
        Bench/site/app target.
        """
        run = self.store.get_run(run_id)
        target = run.target
        if profile.target != target:
            raise RuntimeLifecycleError("credential profile target does not match the run intent")
        if budget is not None and profile.retention_days > budget.retention_days:
            raise RuntimeLifecycleError("credential profile retention exceeds the operational budget")
        return self.store.record_lifecycle_evidence(run_id, "credential_profile", profile.public_record())

    def record_credential_profile_release(
        self, run_id: str, profile: CredentialProfile, *, reason: str = "run_complete",
        budget: OperationalBudget | None = None,
    ) -> LifecycleEvidenceRecord:
        """Record revocation/cleanup without retaining credential material."""
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 120:
            raise RuntimeLifecycleError("credential release reason must be a short non-blank string")
        run = self.store.get_run(run_id)
        if profile.target != run.target:
            raise RuntimeLifecycleError("credential profile target does not match the run intent")
        if budget is not None and profile.retention_days > budget.retention_days:
            raise RuntimeLifecycleError("credential profile retention exceeds the operational budget")
        return self.store.record_lifecycle_evidence(run_id, "credential_profile_released", {
            **profile.public_record(), "reason": reason.strip(),
        })

    def record_attempt(
        self, run_id: str, state: LifecycleState, outcome: AttemptOutcome,
        *, failure_code: str | None = None, policy: RetryPolicy = RetryPolicy(),
    ) -> PersistedLifecycleSnapshot:
        """Persist a typed attempt so restart can make a bounded retry decision."""

        return self.store.record_lifecycle_attempt(
            run_id, state, outcome, failure_code=failure_code, policy=policy,
        )

    def record_proposal_equivalence(
        self, run_id: str, result: NaturalLanguageProposalResult,
    ) -> LifecycleEvidenceRecord:
        """Persist only the typed proof that a proposal reached the approved spec.

        Model output and the natural-language prompt never cross into the
        ledger.  The retained fact is the exact spec digest, bounded attempt
        count, and validator issue codes, all bound to the run intent.
        """

        if not isinstance(result, NaturalLanguageProposalResult):
            raise RuntimeLifecycleError("proposal result must be a typed natural-language result")
        if not result.equivalent or result.escalated or result.spec is None:
            raise RuntimeLifecycleError("proposal is not exact-equivalent and cannot enter run state")
        if not 1 <= len(result.attempts) <= 2:
            raise RuntimeLifecycleError("proposal equivalence must have one or two bounded attempts")
        final = result.attempts[-1].result
        if not final.accepted or final.spec is None:
            raise RuntimeLifecycleError("proposal equivalence lacks an accepted final result")
        digest = spec_hash(result.spec)
        run = self.store.get_run(run_id)
        if digest != run.intent.spec_hash or spec_hash(final.spec) != digest:
            raise RuntimeLifecycleError("proposal equivalence does not bind the run's approved spec")
        issue_codes = tuple(
            issue.code for attempt in result.attempts for issue in attempt.result.issues
        )
        return self.store.record_lifecycle_evidence(run_id, "proposal_equivalence", {
            "proposal_equivalence": "v1",
            "spec_hash": digest,
            "attempt_count": len(result.attempts),
            "issue_codes": list(issue_codes),
        })

    def resume_decision(self, run_id: str, *, policy: RetryPolicy = RetryPolicy()) -> ResumeDecision:
        """Return the only safe next step for a persisted run after restart.

        This is intentionally a decision-only seam: it never invokes a
        provider, appends an attempt, or changes lifecycle state. Callers must
        use the returned action to route through the corresponding typed
        executor, and an interrupted migration remains manual-reconciliation
        only until ``reconcile_migration`` has appended exact evidence.
        """
        run = self.store.get_run(run_id)
        if run.state != RunState.RUNNING:
            raise RuntimeLifecycleError("resume decision requires a running run")
        current = self.store.get_lifecycle_snapshot(run_id)
        if current is None:
            raise RuntimeLifecycleError("resume decision requires a persisted lifecycle snapshot")
        return decide_resume(current.snapshot, policy)

    def reconcile_interrupted_migration(self, run_id: str, *, reconciliation: Mapping[str, object]) -> PersistedLifecycleSnapshot:
        """Compatibility wrapper that still requires exact typed binding."""
        if not isinstance(reconciliation, Mapping) or not reconciliation:
            raise RuntimeLifecycleError("migration reconciliation must be a non-empty object")
        target = reconciliation.get("target")
        if not isinstance(target, Mapping):
            raise RuntimeLifecycleError("migration reconciliation requires an exact target")
        try:
            receipt = MigrationReconciliationReceipt(
                str(reconciliation["reference"]),
                LockTarget(str(target["bench"]), str(target["site"]), str(target["app"])),
                str(reconciliation["spec_hash"]), str(reconciliation["plan_hash"]),
                str(reconciliation.get("outcome", "inspected")),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeLifecycleError("migration reconciliation is not an exact typed receipt") from error
        return self.reconcile_migration(run_id, receipt).snapshot

    def reconcile_migration(
        self, run_id: str, receipt: MigrationReconciliationReceipt,
    ) -> MigrationReconciliationResult:
        """Persist exact external inspection and make retry explicitly eligible."""
        run = self.store.get_run(run_id)
        if receipt.target != run.target or receipt.spec_hash != run.intent.spec_hash or receipt.plan_hash != run.intent.plan_hash:
            raise RuntimeLifecycleError("migration reconciliation does not bind the run intent")
        if receipt.outcome == "confirmed_applied":
            raise RuntimeLifecycleError("confirmed-applied inspection requires post-migration evidence, not retry reconciliation")
        evidence = self.store.record_lifecycle_evidence(run_id, "migration_reconciliation", {
            "reconciliation_receipt": "v1", "reference": receipt.reference,
            "target": {"bench": receipt.target.bench, "site": receipt.target.site, "app": receipt.target.app},
            "spec_hash": receipt.spec_hash, "plan_hash": receipt.plan_hash, "outcome": receipt.outcome,
        })
        snapshot = self.store.reconcile_interrupted_migration(run_id, evidence_id=evidence.id)
        return MigrationReconciliationResult(receipt, evidence, snapshot)

    def record_spec_invalidation(
        self, run_id: str, *, confirmed_spec_hash: str, earliest_state: LifecycleState,
        reason: str = "approved_spec_changed",
    ) -> PersistedLifecycleSnapshot:
        """Persist the invalidated suffix after an approved spec change decision."""

        if not isinstance(confirmed_spec_hash, str) or len(confirmed_spec_hash) != 64 or any(
            character not in "0123456789abcdef" for character in confirmed_spec_hash
        ):
            raise RuntimeLifecycleError("confirmed specification hash must be a SHA-256 digest")
        run = self.store.get_run(run_id)
        if confirmed_spec_hash == run.intent.spec_hash:
            raise RuntimeLifecycleError("spec invalidation requires a changed confirmed specification")
        if not isinstance(earliest_state, LifecycleState):
            raise RuntimeLifecycleError("invalidation state must be a lifecycle state")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 120:
            raise RuntimeLifecycleError("invalidation reason must be a short non-blank string")
        evidence = self.store.record_lifecycle_evidence(run_id, "spec_invalidation", {
            "confirmed_spec_hash": confirmed_spec_hash,
            "earliest_state": earliest_state.value,
            "reason": reason.strip(),
        })
        return self.store.invalidate_lifecycle_from(run_id, earliest_state, evidence_id=evidence.id)

    def record_compilation(
        self, run_id: str, spec: ProjectSpec, compilation: CompilationResult,
        *, proposal_evidence_id: str | None = None,
    ) -> PersistedLifecycleSnapshot:
        run = self.store.get_run(run_id)
        report = validate_project_spec(spec)
        if not report.valid or spec_hash(spec) != run.intent.spec_hash or compilation.spec_hash != run.intent.spec_hash:
            raise RuntimeLifecycleError("validated specification and compilation must bind the run's approved spec")
        audit = audit_compilation(compilation)
        if not audit.valid:
            raise RuntimeLifecycleError("compiled artifacts failed audit")
        validated_payload: dict[str, object] = {"spec_hash": spec_hash(spec)}
        if proposal_evidence_id is not None:
            proposal_evidence = self.store.get_lifecycle_evidence(proposal_evidence_id)
            if proposal_evidence.run_id != run_id or proposal_evidence.kind != "proposal_equivalence":
                raise RuntimeLifecycleError("proposal evidence does not belong to this run")
            proposal_payload = self.store.get_lifecycle_evidence_payload(proposal_evidence_id)
            if proposal_payload.get("proposal_equivalence") != "v1" or proposal_payload.get("spec_hash") != spec_hash(spec):
                raise RuntimeLifecycleError("proposal evidence does not bind the compiled spec")
            validated_payload["proposal_evidence_id"] = proposal_evidence_id
        validated = self.store.record_lifecycle_evidence(run_id, "validated_spec", validated_payload)
        self.store.advance_lifecycle_state(run_id, LifecycleState.SPEC_VALIDATED, (validated.id,))
        compilation_evidence = self.store.record_lifecycle_evidence(run_id, "compilation", {
            "spec_hash": compilation.spec_hash, "ownership_manifest": compilation.ownership_manifest,
        })
        audit_evidence = self.store.record_lifecycle_evidence(run_id, "artifact_audit", {"valid": audit.valid})
        return self.store.advance_lifecycle_state(run_id, LifecycleState.COMPILED, (compilation_evidence.id, audit_evidence.id))

    def record_plan_approval(self, run_id: str, plan: MetadataPlan, approval_id: str) -> PersistedLifecycleSnapshot:
        run = self.store.get_run(run_id)
        approval = self.store.get_approval(approval_id)
        if approval.run_id != run_id or approval.decision != "approved" or approval.intent != run.intent:
            raise RuntimeLifecycleError("approval does not attest to this exact run")
        if plan.spec_hash != run.intent.spec_hash or plan.plan_hash != run.intent.plan_hash or plan.provider != run.intent.provider:
            raise RuntimeLifecycleError("provider plan does not bind the approved run intent")
        plan_evidence = self.store.record_lifecycle_evidence(run_id, "plan", {"plan_hash": plan.plan_hash, "spec_hash": plan.spec_hash, "provider": plan.provider})
        approval_evidence = self.store.record_lifecycle_evidence(run_id, "approval", {"approval_id": approval.id})
        return self.store.advance_lifecycle_state(run_id, LifecycleState.PLAN_APPROVED, (plan_evidence.id, approval_evidence.id))

    def record_migration_gate(
        self, run_id: str, plan: MetadataPlan, compilation: CompilationResult, schema_diff: SchemaDiffReport,
        approval_id: str, backup_evidence_id: str, checkpoint_evidence_id: str,
    ) -> PersistedLifecycleSnapshot:
        run = self.store.get_run(run_id)
        approval = self.store.get_approval(approval_id)
        backup = self.store.get_lifecycle_evidence(backup_evidence_id)
        checkpoint = self.store.get_lifecycle_evidence(checkpoint_evidence_id)
        if backup.run_id != run_id or checkpoint.run_id != run_id:
            raise RuntimeLifecycleError("recovery evidence belongs to another run")
        if backup.kind != "backup" or checkpoint.kind != "checkpoint":
            raise RuntimeLifecycleError("recovery evidence must be recorded as backup and checkpoint")
        backup_payload = self.store.get_lifecycle_evidence_payload(backup_evidence_id)
        checkpoint_payload = self.store.get_lifecycle_evidence_payload(checkpoint_evidence_id)
        if backup_payload.get("recovery_receipt") != "v1" or checkpoint_payload.get("recovery_receipt") != "v1":
            raise RuntimeLifecycleError("backup and checkpoint must come from the typed recovery executor")
        command_id = backup_payload.get("command_receipt_id")
        if not isinstance(command_id, str):
            raise RuntimeLifecycleError("backup evidence lacks a command receipt")
        command = self.store.get_command_receipt(command_id)
        if command.run_id != run_id or command.command != "bench" or command.status != "completed" or command.exit_code != 0:
            raise RuntimeLifecycleError("backup command receipt is not a successful run-bound Bench backup")
        if command.arguments.get("operation") != "SiteBackup":
            raise RuntimeLifecycleError("backup receipt is not a typed SiteBackup operation")
        expected_target = {"bench": run.target.bench, "site": run.target.site, "app": run.target.app}
        if backup_payload.get("target") != expected_target or checkpoint_payload.get("target") != expected_target:
            raise RuntimeLifecycleError("recovery evidence target does not match the run intent")
        if checkpoint_payload.get("spec_hash") != run.intent.spec_hash or checkpoint_payload.get("plan_hash") != run.intent.plan_hash:
            raise RuntimeLifecycleError("source checkpoint does not bind the run intent")
        decision = evaluate_pre_migration_gate(MigrationGateInput(
            run_id, run.intent, plan, compilation, schema_diff, approval,
            MigrationEvidence(backup.reference, checkpoint.reference),
        ))
        if not decision.allowed:
            raise RuntimeLifecycleError("migration gate is blocked: " + ",".join(decision.failure_codes))
        evidence = self.store.record_lifecycle_evidence(run_id, "migration_gate", {
            "plan_hash": plan.plan_hash, "spec_hash": plan.spec_hash,
            "backup": backup.reference, "checkpoint": checkpoint.reference,
        })
        return self.store.advance_lifecycle_state(run_id, LifecycleState.MIGRATION_GATED, (evidence.id, backup_evidence_id, checkpoint_evidence_id))

    def record_frontend_verification(self, run_id: str, *, manifest_hash: str, scenario_count: int) -> PersistedLifecycleSnapshot:
        """Pass frontend verification only after backend evidence exists."""
        if not isinstance(manifest_hash, str) or len(manifest_hash) != 64 or any(
            character not in "0123456789abcdef" for character in manifest_hash
        ):
            raise RuntimeLifecycleError("frontend verification requires a SHA-256 manifest hash")
        if isinstance(scenario_count, bool) or not isinstance(scenario_count, int) or scenario_count < 1:
            raise RuntimeLifecycleError("frontend verification requires at least one scenario")
        current = self.store.get_lifecycle_snapshot(run_id)
        if current is None or current.snapshot.record_for(LifecycleState.BACKEND_VERIFIED).status.value != "passed":
            raise RuntimeLifecycleError("frontend verification requires BACKEND_VERIFIED")
        evidence = self.store.record_lifecycle_evidence(run_id, "frontend_verification", {
            "manifest_hash": manifest_hash, "scenario_count": scenario_count,
        })
        return self.store.advance_lifecycle_state(run_id, LifecycleState.FRONTEND_VERIFIED, (evidence.id,))

    def record_ready(self, run_id: str, *, receipt: Mapping[str, object]) -> PersistedLifecycleSnapshot:
        """Pass READY only with a non-empty, normalized business receipt."""
        if not isinstance(receipt, Mapping) or not receipt:
            raise RuntimeLifecycleError("ready receipt must be a non-empty object")
        current = self.store.get_lifecycle_snapshot(run_id)
        required = (LifecycleState.METADATA_VERIFIED, LifecycleState.BACKEND_VERIFIED, LifecycleState.FRONTEND_VERIFIED)
        if current is None or any(current.snapshot.record_for(state).status.value != "passed" for state in required):
            raise RuntimeLifecycleError("READY requires metadata, backend, and frontend verification")
        evidence = self.store.record_lifecycle_evidence(run_id, "ready_receipt", dict(receipt))
        return self.store.advance_lifecycle_state(run_id, LifecycleState.READY, (evidence.id,))

    def record_business_receipt(self, receipt: BusinessRunReceipt) -> PersistedLifecycleSnapshot:
        """Persist a typed receipt only when it binds to the exact run intent."""
        run = self.store.get_run(receipt.run_id)
        if receipt.spec_hash != run.intent.spec_hash or receipt.plan_hash != run.intent.plan_hash:
            raise RuntimeLifecycleError("business receipt hashes do not match the run intent")
        return self.record_ready(receipt.run_id, receipt=receipt.as_mapping())

    def finalize_success(self, run_id: str):
        """Close a READY run and release its target lock.

        Finalization is deliberately separate from ``record_ready``: READY is
        lifecycle evidence, while SUCCEEDED is the durable run terminal state.
        The transition is allowed only after the current snapshot proves READY;
        ``RunStore.transition`` additionally refuses unfinished commands and
        atomically releases the target lock.
        """
        current = self.store.get_lifecycle_snapshot(run_id)
        if current is None or current.snapshot.record_for(LifecycleState.READY).status.value != "passed":
            raise RuntimeLifecycleError("run finalization requires READY")
        run = self.store.get_run(run_id)
        if run.state != RunState.RUNNING:
            raise RuntimeLifecycleError("run finalization requires a running run")
        return self.store.transition(run_id, RunState.SUCCEEDED)


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value
