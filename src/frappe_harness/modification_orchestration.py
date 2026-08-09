"""Fail-closed assessment of a confirmed specification modification."""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Mapping, Protocol
from .run_store import LifecycleEvidenceRecord, PersistedLifecycleSnapshot, RunIntent, RunRecord, RunStore

from .contracts import ProjectSpec, spec_hash, validate_project_spec
from .lifecycle_contract import InvalidationDecision, LifecycleSnapshot, LifecycleState, invalidate_from
from .provider_plan import MetadataPlan
from .schema_diff import ChangeDisposition, SchemaDiffReport, classify_schema_diff


@dataclass(frozen=True)
class ModificationAssessment:
    installed_spec_hash: str
    confirmed_spec_hash: str
    diff: SchemaDiffReport
    invalidation: InvalidationDecision
    reconfirmed: bool
    allowed: bool
    reason: str | None = None


@dataclass(frozen=True)
class RunBoundModificationResult:
    """Durable assessment identity and optional invalidated lifecycle revision."""

    assessment: ModificationAssessment
    evidence_id: str
    invalidated_snapshot: PersistedLifecycleSnapshot | None


@dataclass(frozen=True)
class ModificationGateReport:
    """Typed V2 governance evidence; approval is deliberately explicit."""

    installed_spec_hash: str
    confirmed_spec_hash: str
    migration_ref: str
    preservation_ref: str
    browser_ref: str
    destructive_no_mutation_ref: str
    approval_ref: str | None = None
    execution_spec_hash: str | None = None
    assessment_ref: str | None = None

    def __post_init__(self) -> None:
        for name, value in (("installed_spec_hash", self.installed_spec_hash), ("confirmed_spec_hash", self.confirmed_spec_hash)):
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"{name} must be a SHA-256 digest")
        for name, value in (("migration_ref", self.migration_ref), ("preservation_ref", self.preservation_ref), ("browser_ref", self.browser_ref), ("destructive_no_mutation_ref", self.destructive_no_mutation_ref)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-blank")
        if self.approval_ref is not None and (not isinstance(self.approval_ref, str) or not self.approval_ref.strip()):
            raise ValueError("approval_ref must be non-blank when supplied")
        if self.execution_spec_hash is not None and (
            len(self.execution_spec_hash) != 64 or any(char not in "0123456789abcdef" for char in self.execution_spec_hash)
        ):
            raise ValueError("execution_spec_hash must be a SHA-256 digest when supplied")
        if self.assessment_ref is not None and (not isinstance(self.assessment_ref, str) or not self.assessment_ref.strip()):
            raise ValueError("assessment_ref must be non-blank when supplied")

    @property
    def status(self) -> str:
        return "approved" if self.approval_ref is not None else "pending"


class AffectedCheckProvider(Protocol):
    """Injected typed V2 check seam; no arbitrary operation names are exposed."""

    def execute(self, run_id: str, check: str) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class AffectedCheckEvidence:
    migration_ref: str
    preservation_ref: str
    browser_ref: str
    destructive_no_mutation_ref: str


class RunBoundAffectedCheckExecutor:
    """Run four fixed V2 checks and persist only normalized same-run evidence."""

    _CHECKS = ("migration", "preservation", "browser", "destructive_no_mutation")

    def __init__(self, store: RunStore, provider: AffectedCheckProvider) -> None:
        self.store = store
        self.provider = provider

    def execute(
        self,
        run_id: str,
        *,
        expected_spec_hash: str | None = None,
        expected_plan_hash: str | None = None,
    ) -> AffectedCheckEvidence:
        run = self.store.get_run(run_id)
        bound_spec_hash = expected_spec_hash or run.intent.spec_hash
        bound_plan_hash = expected_plan_hash or run.intent.plan_hash
        if not re.fullmatch(r"[0-9a-f]{64}", bound_spec_hash) or not re.fullmatch(r"[0-9a-f]{64}", bound_plan_hash):
            raise ValueError("affected checks require SHA-256 spec and plan bindings")
        normalized: list[tuple[str, bool, object | None]] = []
        for check in self._CHECKS:
            try:
                result = self.provider.execute(run_id, check)
            except Exception as error:
                # Preserve the exact bounded phase and a safe diagnostic so a
                # post-backup failure is actionable without retaining REST
                # bodies, cookies, or provider payloads.
                self.store.record_lifecycle_evidence(run_id, "affected_check_failure", {
                    "check": check,
                    "error_type": type(error).__name__,
                    "error": str(error)[:240],
                    "spec_hash": bound_spec_hash,
                    "plan_hash": bound_plan_hash,
                })
                raise
            if not isinstance(result, Mapping) or set(result) - {"passed", "mutation_attempted", "target", "spec_hash", "plan_hash", "browser_attestation"}:
                raise ValueError(f"affected check {check} returned unsupported fields")
            expected_target = {"bench": run.target.bench, "site": run.target.site, "app": run.target.app}
            if result.get("target") != expected_target or result.get("spec_hash") != bound_spec_hash or result.get("plan_hash") != bound_plan_hash:
                raise ValueError(f"affected check {check} is not bound to this run intent")
            if result.get("passed") is not True:
                raise ValueError(f"affected check {check} did not pass")
            # A provider must never attest an unreported mutation on a check
            # that is not the explicit destructive no-mutation probe.  Do not
            # silently discard this field: doing so could turn a mutating
            # migration/preservation/browser implementation into passing
            # evidence.
            if check != "destructive_no_mutation" and result.get("mutation_attempted", False) is not False:
                raise ValueError(f"affected check {check} must attest mutation_attempted=false when supplied")
            if check == "destructive_no_mutation" and result.get("mutation_attempted") is not False:
                raise ValueError("destructive no-mutation check must attest mutation_attempted=false")
            normalized.append((
                check,
                result.get("mutation_attempted", False) if check == "destructive_no_mutation" else False,
                result.get("browser_attestation") if check == "browser" else None,
            ))
        refs: dict[str, str] = {}
        for check, mutation_attempted, browser_attestation in normalized:
            payload = {
                "check": check, "passed": True, "mutation_attempted": mutation_attempted,
                "target": {"bench": run.target.bench, "site": run.target.site, "app": run.target.app},
                "spec_hash": bound_spec_hash, "plan_hash": bound_plan_hash,
            }
            if check == "browser":
                payload["browser_attestation"] = browser_attestation
            evidence = self.store.record_lifecycle_evidence(run_id, f"modification_{check}", payload)
            refs[f"{check}_ref"] = evidence.reference
        return AffectedCheckEvidence(
            refs["migration_ref"], refs["preservation_ref"], refs["browser_ref"], refs["destructive_no_mutation_ref"],
        )


def compose_modification_gate_report(
    assessment: ModificationAssessment,
    *,
    migration_ref: str,
    preservation_ref: str,
    browser_ref: str,
    destructive_no_mutation_ref: str,
    approval_ref: str | None = None,
    execution_spec_hash: str | None = None,
    assessment_ref: str | None = None,
) -> ModificationGateReport:
    """Compose existing V2 evidence without inferring M8 approval."""
    if not assessment.allowed:
        raise ValueError("modification gate requires an allowed safe assessment")
    return ModificationGateReport(
        assessment.installed_spec_hash, assessment.confirmed_spec_hash,
        migration_ref, preservation_ref, browser_ref, destructive_no_mutation_ref,
        approval_ref, execution_spec_hash, assessment_ref,
    )


class RunBoundModificationCoordinator:
    """Bind one confirmed diff to an existing run without mutating a Bench."""

    def __init__(self, store: RunStore) -> None:
        self.store = store

    def assess(
        self,
        run_id: str,
        installed: ProjectSpec,
        confirmed: ProjectSpec,
        *,
        reconfirmed_spec_hash: str | None = None,
    ) -> RunBoundModificationResult:
        from .runtime_lifecycle import RunLifecycleCoordinator

        run = self.store.get_run(run_id)
        if spec_hash(installed) != run.intent.spec_hash:
            raise ValueError("installed specification does not match the run's immutable intent")
        current = self.store.get_lifecycle_snapshot(run_id)
        assessment = assess_modification(
            installed, confirmed, current_snapshot=current.snapshot if current else None,
            reconfirmed_spec_hash=reconfirmed_spec_hash,
        )
        evidence = self.store.record_lifecycle_evidence(run_id, "modification_assessment", {
            "installed_spec_hash": assessment.installed_spec_hash,
            "confirmed_spec_hash": assessment.confirmed_spec_hash,
            "allowed": assessment.allowed,
            "reconfirmed": assessment.reconfirmed,
            "reason": assessment.reason,
            "earliest_state": assessment.invalidation.earliest_state.value,
            "invalidated_states": [state.value for state in assessment.invalidation.invalidated_states],
        })
        snapshot = None
        if assessment.allowed:
            snapshot = RunLifecycleCoordinator(self.store).record_spec_invalidation(
                run_id,
                confirmed_spec_hash=assessment.confirmed_spec_hash,
                earliest_state=assessment.invalidation.earliest_state,
            )
        return RunBoundModificationResult(assessment, evidence.id, snapshot)

    def record_destructive_no_mutation(
        self, run_id: str, assessment: ModificationAssessment,
    ) -> LifecycleEvidenceRecord:
        """Retain proof that a blocked destructive assessment had no mutation path."""

        run = self.store.get_run(run_id)
        if assessment.installed_spec_hash != run.intent.spec_hash:
            raise ValueError("assessment does not bind the run's installed specification")
        if assessment.allowed or assessment.reason != "blocked_schema_diff" or not assessment.diff.blocked:
            raise ValueError("no-mutation evidence requires a blocked assessment")
        return self.store.record_lifecycle_evidence(run_id, "destructive_no_mutation", {
            "installed_spec_hash": assessment.installed_spec_hash,
            "confirmed_spec_hash": assessment.confirmed_spec_hash,
            "blocked_change_codes": [change.code for change in assessment.diff.blocked],
            "mutation_attempted": False,
            "reason": assessment.reason or "blocked_schema_diff",
        })

    def create_revision_run(self, run_id: str, result: RunBoundModificationResult, *, plan: MetadataPlan) -> RunRecord:
        """Create a fresh V2-intent run after exact reconfirmation.

        The parent run is atomically cancelled and its target lock transferred;
        no approval, migration, or provider action is inferred for the
        successor.  The successor must go through its own approval path.
        """
        parent = self.store.get_run(run_id)
        if result.evidence_id == "":
            raise ValueError("revision requires durable modification assessment evidence")
        evidence = self.store.get_lifecycle_evidence(result.evidence_id)
        if evidence.run_id != run_id or evidence.kind != "modification_assessment":
            raise ValueError("revision assessment evidence does not belong to the parent run")
        assessment = result.assessment
        if not assessment.allowed or not assessment.reconfirmed:
            raise ValueError("revision requires an allowed, exactly reconfirmed modification")
        if assessment.installed_spec_hash != parent.intent.spec_hash:
            raise ValueError("revision assessment does not bind the parent intent")
        if plan.spec_hash != assessment.confirmed_spec_hash or plan.provider != parent.intent.provider:
            raise ValueError("revision plan does not bind the confirmed specification/provider")
        return self.store.supersede_with_revision(
            run_id,
            RunIntent(parent.target, plan.provider, plan.spec_hash, plan.plan_hash),
            budget=self.store.get_budget(run_id),
            metadata={
                "revision_of": run_id,
                "revision_from_spec_hash": parent.intent.spec_hash,
                "revision_from_plan_hash": parent.intent.plan_hash,
                "confirmed_spec_hash": assessment.confirmed_spec_hash,
                "invalidation_earliest_state": assessment.invalidation.earliest_state.value,
                "assessment_evidence_id": evidence.id,
            },
        )

    def bind_gate_approval(
        self, run_id: str, report: ModificationGateReport, approval_id: str,
    ) -> ModificationGateReport:
        """Bind approval only to a durable, exact milestone gate record."""
        run = self.store.get_run(run_id)
        for name in ("migration_ref", "preservation_ref", "browser_ref", "destructive_no_mutation_ref"):
            self._require_same_run_evidence(run_id, getattr(report, name), name)
        try:
            approval = self.store.get_milestone_gate_approval(approval_id)
        except Exception as error:
            raise ValueError("approval does not bind this run's exact M8 gate") from error
        if (
            approval.run_id != run_id
            or approval.gate != "MOD-GATE"
            or approval.decision != "approved"
            or approval.intent.target != run.intent.target
            or approval.intent.provider != run.intent.provider
            or approval.intent.spec_hash != (report.execution_spec_hash or report.installed_spec_hash)
            or (report.execution_spec_hash or report.installed_spec_hash) != run.intent.spec_hash
        ):
            raise ValueError("approval does not attest to the exact M8 modification")
        if report.execution_spec_hash and report.execution_spec_hash != report.installed_spec_hash:
            revision_of = run.metadata.get("revision_of")
            if not isinstance(revision_of, str) or not revision_of:
                raise ValueError("successor M8 report must bind revision ancestry")
            parent = self.store.get_run(revision_of)
            if parent.intent.spec_hash != report.installed_spec_hash or run.metadata.get("revision_from_spec_hash") != report.installed_spec_hash:
                raise ValueError("successor M8 report has mismatched parent specification")
            if report.assessment_ref is None or run.metadata.get("assessment_evidence_id") != report.assessment_ref.rsplit(":", 1)[-1]:
                raise ValueError("successor M8 report must bind parent assessment evidence")
        return replace(report, approval_ref=f"milestone_gate:{approval.id}")

    def record_safe_modification_evidence(
        self,
        run_id: str,
        *,
        assessment_ref: str,
        affected: AffectedCheckEvidence,
    ) -> LifecycleEvidenceRecord:
        """Bind the V2 assessment and all four typed checks into one summary."""

        assessment = self.store.get_lifecycle_evidence(assessment_ref.rsplit(":", 1)[-1])
        run = self.store.get_run(run_id)
        same_run = assessment.run_id == run_id
        successor_bound = (
            not same_run
            and run.metadata.get("revision_of") == assessment.run_id
            and run.metadata.get("assessment_evidence_id") == assessment.id
        )
        if assessment.reference != assessment_ref or assessment.kind != "modification_assessment" or not (same_run or successor_bound):
            raise ValueError("safe modification assessment must be same-run or revision-parent modification_assessment evidence")
        refs = {
            "migration": affected.migration_ref,
            "preservation": affected.preservation_ref,
            "browser": affected.browser_ref,
            "destructive_no_mutation": affected.destructive_no_mutation_ref,
        }
        for check, reference in refs.items():
            record = self.store.get_lifecycle_evidence(reference.rsplit(":", 1)[-1])
            if record.run_id != run_id or record.reference != reference or record.kind != f"modification_{check}":
                raise ValueError(f"{check} evidence is not the expected same-run affected-check record")
            payload = self.store.get_lifecycle_evidence_payload(record.id)
            if payload.get("passed") is not True:
                raise ValueError(f"{check} evidence did not pass")
            if check == "destructive_no_mutation" and payload.get("mutation_attempted") is not False:
                raise ValueError("destructive evidence must attest mutation_attempted=false")
        payload = self.store.get_lifecycle_evidence_payload(assessment.id)
        return self.store.record_lifecycle_evidence(run_id, "safe_modification", {
            "version": "v1", "assessment_ref": assessment_ref,
            "assessment_sha256": assessment.sha256,
            "affected_refs": refs,
            "installed_spec_hash": payload.get("installed_spec_hash"),
            "confirmed_spec_hash": payload.get("confirmed_spec_hash"),
        })

    def _require_same_run_evidence(self, run_id: str, reference: str, label: str) -> None:
        match = re.fullmatch(r"run:(?P<run>[^:]+):evidence:(?P<id>[0-9a-f-]+)", reference)
        if match is None or match.group("run") != run_id:
            raise ValueError(f"{label} must be a durable evidence reference for this run")
        try:
            evidence = self.store.get_lifecycle_evidence(match.group("id"))
        except Exception as error:
            raise ValueError(f"{label} does not resolve to durable evidence for this run") from error
        if evidence.run_id != run_id or evidence.reference != reference:
            raise ValueError(f"{label} does not resolve to durable evidence for this run")


@dataclass(frozen=True)
class SuccessorModificationResult:
    migration_evidence: LifecycleEvidenceRecord
    affected: AffectedCheckEvidence
    safe_modification: LifecycleEvidenceRecord


class RunBoundSuccessorModificationExecutor:
    """Execute the V2 migration/check chain after explicit successor approval."""

    def __init__(self, store: RunStore) -> None:
        self.store = store

    def execute(
        self,
        successor_run_id: str,
        *,
        parent_assessment_ref: str,
        installed: ProjectSpec,
        confirmed: ProjectSpec,
        plan: MetadataPlan,
        compilation: object,
        schema_diff: object,
        approval_id: str,
        backup_evidence_id: str,
        checkpoint_evidence_id: str,
        migration_provider: object,
        affected_provider: AffectedCheckProvider,
    ) -> SuccessorModificationResult:
        run = self.store.get_run(successor_run_id)
        if run.metadata.get("assessment_evidence_id") != parent_assessment_ref.rsplit(":", 1)[-1]:
            raise ValueError("successor is not bound to the parent assessment")
        assessment = self.store.get_lifecycle_evidence(parent_assessment_ref.rsplit(":", 1)[-1])
        if assessment.kind != "modification_assessment" or assessment.run_id != run.metadata.get("revision_of"):
            raise ValueError("parent assessment does not match successor ancestry")
        expected = assess_modification(installed, confirmed, reconfirmed_spec_hash=spec_hash(confirmed))
        if not expected.allowed or plan.spec_hash != run.intent.spec_hash:
            raise ValueError("successor plan is not the exact reconfirmed safe V2 plan")
        from .migration_execution import RunBoundMigrationExecutor
        migration = RunBoundMigrationExecutor(self.store).execute(
            successor_run_id, plan=plan, compilation=compilation, schema_diff=schema_diff,
            approval_id=approval_id, backup_evidence_id=backup_evidence_id,
            checkpoint_evidence_id=checkpoint_evidence_id, provider=migration_provider,
        )
        affected = RunBoundAffectedCheckExecutor(self.store, affected_provider).execute(
            successor_run_id, expected_spec_hash=run.intent.spec_hash, expected_plan_hash=run.intent.plan_hash,
        )
        safe = RunBoundModificationCoordinator(self.store).record_safe_modification_evidence(
            successor_run_id, assessment_ref=parent_assessment_ref, affected=affected,
        )
        return SuccessorModificationResult(migration.evidence, affected, safe)


def assess_modification(
    installed: ProjectSpec,
    confirmed: ProjectSpec,
    *,
    current_snapshot: LifecycleSnapshot | None = None,
    reconfirmed_spec_hash: str | None = None,
) -> ModificationAssessment:
    """Return a reviewable diff and never infer approval from a safe diff."""
    installed_hash = spec_hash(installed)
    confirmed_hash = spec_hash(confirmed)
    diff = classify_schema_diff(installed, confirmed)
    invalid = _invalidated_states(diff, current_snapshot)
    reconfirmed = reconfirmed_spec_hash == confirmed_hash
    if not validate_project_spec(installed).valid or not validate_project_spec(confirmed).valid:
        return ModificationAssessment(installed_hash, confirmed_hash, diff, invalid, reconfirmed, False, "invalid_spec")
    if diff.blocked:
        return ModificationAssessment(installed_hash, confirmed_hash, diff, invalid, reconfirmed, False, "blocked_schema_diff")
    if not reconfirmed:
        return ModificationAssessment(installed_hash, confirmed_hash, diff, invalid, False, False, "reconfirmation_required")
    return ModificationAssessment(installed_hash, confirmed_hash, diff, invalid, True, True)


def _invalidated_states(diff: SchemaDiffReport, snapshot: LifecycleSnapshot | None) -> InvalidationDecision:
    earliest = LifecycleState.FRONTEND_VERIFIED
    if any(change.disposition is ChangeDisposition.SAFE_ADDITIVE for change in diff.changes):
        earliest = LifecycleState.MIGRATION_GATED
    elif any(change.code.startswith("entity_") or change.code.startswith("field_") for change in diff.changes):
        earliest = LifecycleState.METADATA_VERIFIED
    base = snapshot or LifecycleSnapshot("0" * 64)
    return invalidate_from(base, earliest)
