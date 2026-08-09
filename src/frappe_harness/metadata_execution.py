"""Run-bound metadata verification and lifecycle admission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .metadata_verifier import MetadataVerificationReport, verify_metadata_plan
from .provider_plan import MetadataPlan
from .run_store import (
    InvalidStateTransition,
    LifecycleEvidenceRecord,
    PersistedLifecycleSnapshot,
    RunState,
    RunStore,
)
from .lifecycle_contract import LifecycleState


class MetadataVerificationAdmissionError(RuntimeError):
    """Metadata observation cannot be bound to this run."""


@dataclass(frozen=True)
class RunBoundMetadataVerificationResult:
    report: MetadataVerificationReport
    evidence: LifecycleEvidenceRecord
    snapshot: PersistedLifecycleSnapshot | None


class RunBoundMetadataVerifier:
    """Compare observed metadata and pass only an exact matching plan."""

    def __init__(self, store: RunStore) -> None:
        self.store = store

    def verify(
        self,
        run_id: str,
        *,
        plan: MetadataPlan,
        observed_doctypes: Mapping[str, Mapping[str, Any]],
        provider_receipt_evidence_ids: tuple[str, ...] = (),
    ) -> RunBoundMetadataVerificationResult:
        run = self.store.get_run(run_id)
        if run.state is not RunState.RUNNING:
            raise MetadataVerificationAdmissionError("metadata verification requires a running approved run")
        if plan.spec_hash != run.intent.spec_hash or plan.plan_hash != run.intent.plan_hash:
            raise MetadataVerificationAdmissionError("metadata plan does not bind the run's immutable intent")
        snapshot = self.store.get_lifecycle_snapshot(run_id)
        if snapshot is None or snapshot.snapshot.record_for(LifecycleState.MIGRATION_APPLIED).status.value != "passed":
            raise MetadataVerificationAdmissionError("metadata verification requires MIGRATION_APPLIED")
        self._require_bound_migration_receipt(run_id, run.intent, plan, snapshot)
        if provider_receipt_evidence_ids:
            self._require_provider_receipts(run_id, provider_receipt_evidence_ids, len(plan.operations))
        report = verify_metadata_plan(plan, observed_doctypes)
        evidence = self.store.record_lifecycle_evidence(run_id, "metadata_verification", {
            "plan_hash": plan.plan_hash,
            "matches": report.matches,
            "mismatch_count": len(report.mismatches),
            "provider_receipt_evidence_ids": list(provider_receipt_evidence_ids),
        })
        if not report.matches:
            return RunBoundMetadataVerificationResult(report, evidence, None)
        try:
            advanced = self.store.advance_lifecycle_state(run_id, LifecycleState.METADATA_VERIFIED, (evidence.id,))
        except InvalidStateTransition as error:
            raise MetadataVerificationAdmissionError(str(error)) from error
        return RunBoundMetadataVerificationResult(report, evidence, advanced)

    def _require_provider_receipts(self, run_id: str, evidence_ids: tuple[str, ...], expected_count: int) -> None:
        if len(evidence_ids) != expected_count or len(set(evidence_ids)) != len(evidence_ids):
            raise MetadataVerificationAdmissionError("metadata verification requires one receipt per planned provider operation")
        for evidence_id in evidence_ids:
            try:
                evidence = self.store.get_lifecycle_evidence(evidence_id)
                payload = self.store.get_lifecycle_evidence_payload(evidence_id)
            except Exception as error:
                raise MetadataVerificationAdmissionError("provider receipt evidence is unavailable") from error
            if evidence.run_id != run_id or evidence.kind != "frappectl_receipt":
                raise MetadataVerificationAdmissionError("provider receipt evidence is not bound to this run")
            if not isinstance(payload.get("argv"), list) or payload.get("exit_code") != 0:
                raise MetadataVerificationAdmissionError("provider receipt evidence is not a successful redacted receipt")

    def _require_bound_migration_receipt(self, run_id, intent, plan, snapshot) -> None:
        """Require the passed migration state to carry a successful exact receipt."""
        records = snapshot.snapshot.record_for(LifecycleState.MIGRATION_APPLIED).evidence
        migration = [record for record in records if record.kind == "migration_receipt"]
        if len(migration) != 1 or ":evidence:" not in migration[0].reference:
            raise MetadataVerificationAdmissionError("MIGRATION_APPLIED lacks one bound migration receipt")
        evidence_id = migration[0].reference.rsplit(":evidence:", 1)[1]
        try:
            payload = self.store.get_lifecycle_evidence_payload(evidence_id)
            command_id = payload.get("command_receipt_id")
            command = self.store.get_command_receipt(command_id) if isinstance(command_id, str) else None
        except Exception as error:
            raise MetadataVerificationAdmissionError("migration receipt evidence is unavailable") from error
        target = {"bench": intent.target.bench, "site": intent.target.site, "app": intent.target.app}
        if (
            payload.get("provider") != intent.provider
            or payload.get("plan_hash") != plan.plan_hash
            or not isinstance(payload.get("provider_reference"), str)
            or not payload["provider_reference"].strip()
            or command is None
            or command.run_id != run_id
            or command.command != "migration-provider"
            or command.status != "completed"
            or command.exit_code != 0
            or command.arguments != {"provider": intent.provider, "plan_hash": plan.plan_hash, "target": target}
        ):
            raise MetadataVerificationAdmissionError("migration receipt does not bind the exact successful provider run")
