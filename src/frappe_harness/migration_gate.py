"""Pure, fail-closed admission control for a schema-changing run.

The gate deliberately does not inspect a Bench, persist a decision, execute a
provider, or trust a path as proof of a backup.  It only combines immutable
inputs already gathered by those boundaries into an auditable decision.  A
caller may proceed to its provider only when ``allowed`` is true.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .artifact_audit import ArtifactAuditReport, audit_compilation
from .compiler import CompilationResult
from .provider_plan import MetadataPlan
from .run_store import ApprovalRecord, RunIntent
from .schema_diff import SchemaDiffReport


@dataclass(frozen=True)
class MigrationEvidence:
    """Opaque, externally recorded evidence required before mutation.

    References are opaque identifiers rather than paths or shell commands.
    Their existence and target binding are verified by the environment adapter,
    not this pure policy function.
    """

    backup_id: str
    checkpoint_id: str


@dataclass(frozen=True)
class MigrationGateInput:
    """All immutable inputs that must agree before a provider may mutate."""

    run_id: str
    intent: RunIntent
    plan: MetadataPlan
    compilation: CompilationResult
    schema_diff: SchemaDiffReport
    approval: ApprovalRecord | None
    evidence: MigrationEvidence | None


@dataclass(frozen=True)
class MigrationGateFailure:
    code: str
    message: str


@dataclass(frozen=True)
class MigrationGateDecision:
    """Structured policy output; no decision is implicitly permissive."""

    allowed: bool
    failures: tuple[MigrationGateFailure, ...]
    artifact_audit: ArtifactAuditReport

    @property
    def failure_codes(self) -> tuple[str, ...]:
        return tuple(failure.code for failure in self.failures)


def evaluate_pre_migration_gate(input: MigrationGateInput) -> MigrationGateDecision:
    """Return a fail-closed decision for one exact immutable run intent.

    The compiler output, provider plan, approval, and diff must all bind to the
    same confirmed spec and plan hashes.  The exact approved run id, backup,
    checkpoint, a clean artifact audit, and a non-blocked diff are mandatory.
    ``MigrationEvidence`` only proves that references were supplied; the later
    environment adapter must verify those references before executing anything.
    """

    failures: list[MigrationGateFailure] = []
    audit = audit_compilation(input.compilation)
    if not input.run_id.strip():
        _fail(failures, "missing_run_id", "a migration decision must name an existing run")
    if input.compilation.spec_hash != input.intent.spec_hash:
        _fail(failures, "compilation_spec_hash_mismatch", "compiled artifacts do not bind to the run's confirmed spec hash")
    if input.plan.spec_hash != input.intent.spec_hash:
        _fail(failures, "plan_spec_hash_mismatch", "provider plan does not bind to the run's confirmed spec hash")
    if input.plan.plan_hash != input.intent.plan_hash:
        _fail(failures, "plan_hash_mismatch", "provider plan does not bind to the run's approved plan hash")
    if input.schema_diff.confirmed_spec_hash != input.intent.spec_hash:
        _fail(failures, "diff_spec_hash_mismatch", "schema diff is not bound to the run's confirmed spec hash")
    if not input.schema_diff.installed_spec_hash:
        _fail(failures, "missing_installed_schema_identity", "schema diff must identify the observed installed schema")
    if input.schema_diff.blocked:
        _fail(failures, "blocked_schema_diff", "schema diff contains destructive or unsupported changes")
    for issue in audit.issues:
        _fail(failures, f"artifact_{issue.code}", f"artifact audit failed at {issue.path}: {issue.message}")
    _check_approval(input, failures)
    _check_evidence(input.evidence, failures)
    return MigrationGateDecision(not failures, tuple(failures), audit)


def _check_approval(input: MigrationGateInput, failures: list[MigrationGateFailure]) -> None:
    approval = input.approval
    if approval is None:
        _fail(failures, "missing_approval", "an exact-intent approval is required")
        return
    if approval.run_id != input.run_id:
        _fail(failures, "approval_run_mismatch", "approval does not name this run")
    if approval.intent != input.intent:
        _fail(failures, "approval_intent_mismatch", "approval does not attest to this exact target, provider, spec, and plan")
    if approval.decision != "approved":
        _fail(failures, "approval_not_approved", "approval decision is not approved")
    if not isinstance(approval.approver, str) or not approval.approver.strip():
        _fail(failures, "missing_approver", "approval must identify a non-blank human or authority")


def _check_evidence(evidence: MigrationEvidence | None, failures: list[MigrationGateFailure]) -> None:
    if evidence is None:
        _fail(failures, "missing_migration_evidence", "backup and source checkpoint evidence are required")
        return
    if not isinstance(evidence.backup_id, str) or not evidence.backup_id.strip():
        _fail(failures, "missing_backup", "a non-blank backup evidence identifier is required")
    if not isinstance(evidence.checkpoint_id, str) or not evidence.checkpoint_id.strip():
        _fail(failures, "missing_checkpoint", "a non-blank source checkpoint identifier is required")


def _fail(failures: list[MigrationGateFailure], code: str, message: str) -> None:
    failures.append(MigrationGateFailure(code, message))
