"""Durable, local persistence for controlled harness runs.

This module deliberately uses only :mod:`sqlite3`: the run ledger is an
authority boundary and should not need an ORM or a running service to retain
state.  Every mutating operation is transactional.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Mapping
from uuid import uuid4
import re

from .lifecycle_contract import (
    AttemptOutcome, Evidence, LifecycleError, LifecycleSnapshot, LifecycleState, RetryPolicy,
    apply_invalidation, record_attempt, record_pass, reconcile_interrupted,
)
from .verification_program import VerificationProgram, VerificationRunReport
from .operational_budget import OperationalBudget


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _output_bytes(output: str | bytes) -> bytes:
    """Normalize a process output value without retaining its text."""
    if isinstance(output, str):
        return output.encode("utf-8")
    if isinstance(output, bytes):
        return output
    raise TypeError("command output must be str or bytes")


def _budget_payload(budget: OperationalBudget) -> dict[str, Any]:
    if not isinstance(budget, OperationalBudget):
        raise TypeError("budget must be an OperationalBudget")
    return {
        "command_timeout_seconds": budget.command_timeout_seconds,
        "model_timeout_seconds": budget.model_timeout_seconds,
        "max_artifact_bytes": budget.max_artifact_bytes,
        "max_log_bytes": budget.max_log_bytes,
        "max_retries": budget.max_retries,
        "retention_days": budget.retention_days,
        "max_model_tokens": budget.max_model_tokens,
        "max_model_cost_usd": budget.max_model_cost_usd,
    }


def _budget_json(budget: OperationalBudget) -> str:
    return json.dumps(_budget_payload(budget), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class RunStoreError(RuntimeError):
    """Base error for persistent run-store failures."""


class RunNotFound(RunStoreError):
    """Raised when a referenced run does not exist."""


class LockConflict(RunStoreError):
    """Raised when another active run owns the requested target lock."""


class InvalidStateTransition(RunStoreError):
    """Raised when a run is asked to make an illegal state transition."""


class IntentMismatch(RunStoreError):
    """Raised when an approval does not attest to the run's exact intent."""


class ApprovalRequired(RunStoreError):
    """Raised when execution is attempted without a persisted matching approval."""


class RunState(str, Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


_ALLOWED_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.DRAFT: frozenset({RunState.PENDING_APPROVAL, RunState.CANCELLED}),
    RunState.PENDING_APPROVAL: frozenset({RunState.APPROVED, RunState.CANCELLED}),
    RunState.APPROVED: frozenset({RunState.RUNNING, RunState.CANCELLED}),
    RunState.RUNNING: frozenset({RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}),
    RunState.SUCCEEDED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
}


@dataclass(frozen=True)
class LockTarget:
    """The exact Bench/site/app environment a run may mutate."""

    bench: str
    site: str
    app: str

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (self.bench, self.site, self.app)):
            raise ValueError("bench, site, and app must be non-empty strings")


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True)
class RunIntent:
    """Immutable, approval-bound identity of the work a run is authorized to do."""

    target: LockTarget
    provider: str
    spec_hash: str
    plan_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not _PROVIDER_RE.fullmatch(self.provider):
            raise ValueError("provider must be a lower-case provider identifier")
        for name, value in (("spec_hash", self.spec_hash), ("plan_hash", self.plan_hash)):
            if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
                raise ValueError(f"{name} must be a 64-character lower-case SHA-256 hex digest")


@dataclass(frozen=True)
class RunRecord:
    id: str
    target: LockTarget
    state: RunState
    created_at: str
    updated_at: str
    metadata: Mapping[str, Any]
    intent: RunIntent


@dataclass(frozen=True)
class ApprovalRecord:
    id: str
    run_id: str
    approver: str
    decision: str
    rationale: str | None
    created_at: str
    intent: RunIntent


@dataclass(frozen=True)
class MilestoneGateApprovalRecord:
    """Immutable explicit approval for one governance gate and run intent."""

    id: str
    run_id: str
    gate: str
    approver: str
    decision: str
    created_at: str
    intent: RunIntent


@dataclass(frozen=True)
class CommandReceipt:
    """Audit summary for a command execution.

    Process output is deliberately absent.  Command output often contains
    credentials, document data, or implementation details that do not belong
    in a durable control-plane ledger.  The byte count and SHA-256 digest make
    the result independently auditable when an operator has the original,
    separately protected execution log.
    """

    id: str
    run_id: str
    command: str
    arguments: Mapping[str, Any]
    status: str
    stdout_bytes: int
    stdout_sha256: str
    stderr_bytes: int
    stderr_sha256: str
    exit_code: int | None
    created_at: str
    completed_at: str | None


@dataclass(frozen=True)
class PersistedLifecycleSnapshot:
    """One append-only, content-addressed lifecycle snapshot revision."""

    run_id: str
    revision: int
    snapshot: LifecycleSnapshot
    sha256: str
    created_at: str


@dataclass(frozen=True)
class VerificationReportRecord:
    """A normalized verification result, deliberately without probe payloads."""

    id: str
    run_id: str
    spec_hash: str
    template_hash: str
    sha256: str
    created_at: str

    @property
    def reference(self) -> str:
        return f"run:{self.run_id}:verification:{self.id}"


@dataclass(frozen=True)
class LifecycleEvidenceRecord:
    """Immutable normalized fact used to pass one lifecycle state."""

    id: str
    run_id: str
    kind: str
    sha256: str
    created_at: str

    @property
    def reference(self) -> str:
        return f"run:{self.run_id}:evidence:{self.id}"


class RunStore:
    """SQLite-backed ledger with one exclusive lock per Bench/site/app tuple."""

    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        self._initialize()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._transaction() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    bench TEXT NOT NULL,
                    site TEXT NOT NULL,
                    app TEXT NOT NULL,
                    state TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    spec_hash TEXT NOT NULL,
                    plan_hash TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS target_locks (
                    bench TEXT NOT NULL,
                    site TEXT NOT NULL,
                    app TEXT NOT NULL,
                    run_id TEXT NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
                    acquired_at TEXT NOT NULL,
                    PRIMARY KEY (bench, site, app)
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    approver TEXT NOT NULL,
                    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
                    rationale TEXT,
                    bench TEXT NOT NULL,
                    site TEXT NOT NULL,
                    app TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    spec_hash TEXT NOT NULL,
                    plan_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS milestone_gate_approvals (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    gate TEXT NOT NULL,
                    approver TEXT NOT NULL,
                    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
                    bench TEXT NOT NULL,
                    site TEXT NOT NULL,
                    app TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    spec_hash TEXT NOT NULL,
                    plan_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS command_receipts (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    command TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('started', 'completed', 'failed')),
                    stdout_bytes INTEGER NOT NULL DEFAULT 0,
                    stdout_sha256 TEXT NOT NULL DEFAULT '',
                    stderr_bytes INTEGER NOT NULL DEFAULT 0,
                    stderr_sha256 TEXT NOT NULL DEFAULT '',
                    exit_code INTEGER,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS lifecycle_snapshots (
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL CHECK (revision > 0),
                    intent_hash TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, revision)
                );
                CREATE TABLE IF NOT EXISTS verification_reports (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    spec_hash TEXT NOT NULL,
                    template_hash TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    report_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lifecycle_evidence (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS run_budgets (
                    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
                    budget_json TEXT NOT NULL,
                    budget_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._add_legacy_columns(conn)
            self._redact_legacy_receipt_output(conn)
            conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS runs_intent_is_immutable
                BEFORE UPDATE OF bench, site, app, provider, spec_hash, plan_hash ON runs
                BEGIN
                    SELECT RAISE(ABORT, 'run intent is immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS approvals_intent_is_immutable
                BEFORE UPDATE ON approvals
                BEGIN
                    SELECT RAISE(ABORT, 'approval record is immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS milestone_gate_approvals_are_immutable
                BEFORE UPDATE ON milestone_gate_approvals
                BEGIN
                    SELECT RAISE(ABORT, 'milestone gate approval is immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS lifecycle_snapshots_are_append_only_update
                BEFORE UPDATE ON lifecycle_snapshots
                BEGIN
                    SELECT RAISE(ABORT, 'lifecycle snapshots are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS lifecycle_snapshots_are_append_only_delete
                BEFORE DELETE ON lifecycle_snapshots
                BEGIN
                    SELECT RAISE(ABORT, 'lifecycle snapshots are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS verification_reports_are_append_only_update
                BEFORE UPDATE ON verification_reports
                BEGIN
                    SELECT RAISE(ABORT, 'verification reports are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS verification_reports_are_append_only_delete
                BEFORE DELETE ON verification_reports
                BEGIN
                    SELECT RAISE(ABORT, 'verification reports are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS lifecycle_evidence_is_append_only_update
                BEFORE UPDATE ON lifecycle_evidence
                BEGIN
                    SELECT RAISE(ABORT, 'lifecycle evidence is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS lifecycle_evidence_is_append_only_delete
                BEFORE DELETE ON lifecycle_evidence
                BEGIN
                    SELECT RAISE(ABORT, 'lifecycle evidence is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS run_budgets_are_immutable_update
                BEFORE UPDATE ON run_budgets
                BEGIN
                    SELECT RAISE(ABORT, 'run budget is immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS run_budgets_are_immutable_delete
                BEFORE DELETE ON run_budgets
                BEGIN
                    SELECT RAISE(ABORT, 'run budget is immutable');
                END;
                """
            )

    @staticmethod
    def _add_legacy_columns(conn: sqlite3.Connection) -> None:
        """Keep old local ledgers readable, while refusing them as unbound runs.

        SQLite cannot add a NOT NULL column to an already-populated table
        without an unsafe invented default.  Columns added here therefore stay
        NULL for pre-intent rows, which ``_run_from_row`` rejects explicitly.
        """
        for table, columns in {
            "runs": {"provider", "spec_hash", "plan_hash"},
            "approvals": {"bench", "site", "app", "provider", "spec_hash", "plan_hash"},
            "command_receipts": {"stdout_bytes", "stdout_sha256", "stderr_bytes", "stderr_sha256"},
        }.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column in columns - existing:
                column_type = "INTEGER" if column.endswith("_bytes") else "TEXT"
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

    @staticmethod
    def _redact_legacy_receipt_output(conn: sqlite3.Connection) -> None:
        """Migrate prior ledgers to digest-only receipts and erase raw output.

        Older builds persisted ``stdout`` and ``stderr``.  We retain their
        length/digest evidence while clearing those legacy columns in the same
        initialization transaction.  Fresh databases never create them.
        """
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(command_receipts)")}
        required = {"stdout", "stderr"}
        if not required.issubset(columns):
            return
        rows = conn.execute(
            "SELECT id, stdout, stderr, stdout_bytes, stdout_sha256, stderr_bytes, stderr_sha256 "
            "FROM command_receipts"
        ).fetchall()
        for row in rows:
            stdout = _output_bytes(row["stdout"] or "")
            stderr = _output_bytes(row["stderr"] or "")
            # Prefer the existing digest summary if this ledger was already
            # migrated; otherwise derive it before erasing the raw content.
            stdout_bytes = row["stdout_bytes"] if row["stdout_sha256"] else len(stdout)
            stdout_digest = row["stdout_sha256"] or hashlib.sha256(stdout).hexdigest()
            stderr_bytes = row["stderr_bytes"] if row["stderr_sha256"] else len(stderr)
            stderr_digest = row["stderr_sha256"] or hashlib.sha256(stderr).hexdigest()
            conn.execute(
                "UPDATE command_receipts SET stdout_bytes = ?, stdout_sha256 = ?, "
                "stderr_bytes = ?, stderr_sha256 = ?, stdout = '', stderr = '' WHERE id = ?",
                (stdout_bytes, stdout_digest, stderr_bytes, stderr_digest, row["id"]),
            )

    def create_run(self, intent: RunIntent, *, metadata: Mapping[str, Any] | None = None,
                   run_id: str | None = None, budget: OperationalBudget | None = None) -> RunRecord:
        """Create a draft run and atomically reserve its target environment."""
        budget = budget or OperationalBudget()
        budget_text = _budget_json(budget)
        budget_digest = hashlib.sha256(budget_text.encode("utf-8")).hexdigest()
        run_id = run_id or str(uuid4())
        now = _utc_now()
        metadata_json = json.dumps(dict(metadata or {}), sort_keys=True)
        try:
            with self._transaction() as conn:
                conn.execute(
                    "INSERT INTO runs (id, bench, site, app, state, provider, spec_hash, plan_hash, metadata_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, intent.target.bench, intent.target.site, intent.target.app, RunState.DRAFT.value,
                     intent.provider, intent.spec_hash, intent.plan_hash, metadata_json, now, now),
                )
                conn.execute(
                    "INSERT INTO target_locks VALUES (?, ?, ?, ?, ?)",
                    (intent.target.bench, intent.target.site, intent.target.app, run_id, now),
                )
                conn.execute(
                    "INSERT INTO run_budgets (run_id, budget_json, budget_sha256, created_at) VALUES (?, ?, ?, ?)",
                    (run_id, budget_text, budget_digest, now),
                )
        except sqlite3.IntegrityError as error:
            if "target_locks.bench, target_locks.site, target_locks.app" in str(error):
                target = intent.target
                raise LockConflict(f"target already locked: {target.bench}/{target.site}/{target.app}") from error
            raise RunStoreError(f"could not create run {run_id}") from error
        return RunRecord(run_id, intent.target, RunState.DRAFT, now, now, json.loads(metadata_json), intent)

    def supersede_with_revision(
        self, parent_run_id: str, intent: RunIntent, *, metadata: Mapping[str, Any] | None = None,
        budget: OperationalBudget | None = None,
    ) -> RunRecord:
        """Atomically transfer one active target lock to a confirmed revision run.

        The parent intent and evidence remain immutable.  Only a running parent
        with no started command may be superseded; its lock is released in the
        same transaction that creates the V2 successor, so two runs can never
        own the target concurrently.
        """
        successor_id = str(uuid4())
        now = _utc_now()
        budget = budget or OperationalBudget()
        budget_text = _budget_json(budget)
        budget_digest = hashlib.sha256(budget_text.encode("utf-8")).hexdigest()
        with self._transaction() as conn:
            parent_row = conn.execute("SELECT * FROM runs WHERE id = ?", (parent_run_id,)).fetchone()
            if parent_row is None:
                raise RunNotFound(parent_run_id)
            parent = self._run_from_row(parent_row)
            if parent.state is not RunState.RUNNING:
                raise InvalidStateTransition("revision handoff requires a running parent run")
            if intent.target != parent.target:
                raise IntentMismatch("revision successor target does not match the parent run")
            if intent.spec_hash == parent.intent.spec_hash and intent.plan_hash == parent.intent.plan_hash:
                raise IntentMismatch("revision successor must change the run intent")
            if conn.execute(
                "SELECT 1 FROM command_receipts WHERE run_id = ? AND status = 'started'", (parent_run_id,)
            ).fetchone() is not None:
                raise InvalidStateTransition("cannot hand off a revision while a command receipt is started")
            successor_metadata = dict(metadata or {})
            successor_metadata.setdefault("revision_of", parent_run_id)
            metadata_json = json.dumps(successor_metadata, sort_keys=True)
            conn.execute(
                "UPDATE runs SET state = ?, updated_at = ? WHERE id = ?",
                (RunState.CANCELLED.value, now, parent_run_id),
            )
            conn.execute("DELETE FROM target_locks WHERE run_id = ?", (parent_run_id,))
            try:
                conn.execute(
                    "INSERT INTO runs (id, bench, site, app, state, provider, spec_hash, plan_hash, metadata_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (successor_id, intent.target.bench, intent.target.site, intent.target.app, RunState.DRAFT.value,
                     intent.provider, intent.spec_hash, intent.plan_hash, metadata_json, now, now),
                )
                conn.execute(
                    "INSERT INTO target_locks VALUES (?, ?, ?, ?, ?)",
                    (intent.target.bench, intent.target.site, intent.target.app, successor_id, now),
                )
                conn.execute(
                    "INSERT INTO run_budgets (run_id, budget_json, budget_sha256, created_at) VALUES (?, ?, ?, ?)",
                    (successor_id, budget_text, budget_digest, now),
                )
            except sqlite3.IntegrityError as error:
                raise LockConflict("revision target lock could not be transferred") from error
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (successor_id,)).fetchone()
        return self._run_from_row(row)

    def get_budget(self, run_id: str) -> OperationalBudget | None:
        """Return the immutable budget snapshot, or ``None`` for legacy runs."""
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is None:
                raise RunNotFound(run_id)
            row = conn.execute("SELECT budget_json, budget_sha256 FROM run_budgets WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        payload = json.loads(row["budget_json"])
        if not isinstance(payload, dict):
            raise RunStoreError(f"run budget {run_id} is not a JSON object")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != row["budget_sha256"]:
            raise RunStoreError(f"run budget {run_id} digest mismatch")
        try:
            return OperationalBudget(**payload)
        except (TypeError, ValueError) as error:
            raise RunStoreError(f"run budget {run_id} is invalid") from error

    def persist_budget(self, run_id: str, budget: OperationalBudget) -> None:
        """Bind a budget once to a draft run, for migrated callers."""
        budget_text = _budget_json(budget)
        digest = hashlib.sha256(budget_text.encode("utf-8")).hexdigest()
        with self._transaction() as conn:
            row = conn.execute("SELECT state FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunNotFound(run_id)
            if row["state"] != RunState.DRAFT.value:
                raise InvalidStateTransition("run budget may only be bound to a draft run")
            try:
                conn.execute("INSERT INTO run_budgets (run_id, budget_json, budget_sha256, created_at) VALUES (?, ?, ?, ?)",
                             (run_id, budget_text, digest, _utc_now()))
            except sqlite3.IntegrityError as error:
                raise RunStoreError("run already has an immutable budget") from error

    def get_run(self, run_id: str) -> RunRecord:
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFound(run_id)
        return self._run_from_row(row)

    def transition(self, run_id: str, to_state: RunState) -> RunRecord:
        """Move a run through its finite-state lifecycle and release terminal locks."""
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunNotFound(run_id)
            from_state = RunState(row["state"])
            if to_state not in _ALLOWED_TRANSITIONS[from_state]:
                raise InvalidStateTransition(f"cannot transition {run_id} from {from_state.value} to {to_state.value}")
            if to_state in {RunState.APPROVED, RunState.RUNNING} and not self._has_matching_approval(conn, row):
                raise ApprovalRequired(f"cannot transition {run_id} to {to_state.value} without a matching persisted approval")
            if not _ALLOWED_TRANSITIONS[to_state] and conn.execute(
                "SELECT 1 FROM command_receipts WHERE run_id = ? AND status = 'started'", (run_id,)
            ).fetchone() is not None:
                raise InvalidStateTransition(
                    f"cannot transition {run_id} to {to_state.value} while a command receipt is still started"
                )
            now = _utc_now()
            conn.execute("UPDATE runs SET state = ?, updated_at = ? WHERE id = ?", (to_state.value, now, run_id))
            if not _ALLOWED_TRANSITIONS[to_state]:
                conn.execute("DELETE FROM target_locks WHERE run_id = ?", (run_id,))
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._run_from_row(row)

    def record_approval(
        self,
        run_id: str,
        *,
        approver: str,
        approved: bool,
        intent: RunIntent,
        rationale: str | None = None,
    ) -> ApprovalRecord:
        """Record an approval only when it names the run's exact immutable intent."""
        if not approver.strip():
            raise ValueError("approver must not be blank")
        record = ApprovalRecord(str(uuid4()), run_id, approver, "approved" if approved else "rejected", rationale, _utc_now(), intent)
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunNotFound(run_id)
            run = self._run_from_row(row)
            if not _ALLOWED_TRANSITIONS[run.state]:
                raise InvalidStateTransition(
                    f"cannot record approval for terminal run {run_id} in state {run.state.value}"
                )
            if intent != run.intent:
                raise IntentMismatch("approval intent does not match the run's immutable intent")
            conn.execute(
                "INSERT INTO approvals (id, run_id, approver, decision, rationale, bench, site, app, provider, spec_hash, plan_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record.id, record.run_id, record.approver, record.decision, record.rationale,
                 intent.target.bench, intent.target.site, intent.target.app, intent.provider,
                 intent.spec_hash, intent.plan_hash, record.created_at),
            )
        return record

    def get_approval(self, approval_id: str) -> ApprovalRecord:
        """Read the immutable intent snapshot an approval attested to."""
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            raise RunNotFound(f"approval {approval_id}")
        return self._approval_from_row(row)

    def record_milestone_gate_approval(
        self, run_id: str, *, gate: str, approver: str, approved: bool, intent: RunIntent,
    ) -> MilestoneGateApprovalRecord:
        """Persist an explicit gate approval bound to the exact run intent."""
        from .milestone_gates import GATES

        if gate not in GATES:
            raise ValueError("unknown milestone gate")
        if not isinstance(approver, str) or not approver.strip():
            raise ValueError("gate approver must not be blank")
        record = MilestoneGateApprovalRecord(
            str(uuid4()), run_id, gate, approver.strip(), "approved" if approved else "rejected", _utc_now(), intent,
        )
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunNotFound(run_id)
            run = self._run_from_row(row)
            # Governance may be recorded after successful execution. Keep
            # exact intent binding and reject failed/cancelled terminal runs.
            if run.state not in {RunState.SUCCEEDED} and not _ALLOWED_TRANSITIONS[run.state]:
                raise InvalidStateTransition(
                    f"cannot record milestone gate approval for terminal run {run_id} in state {run.state.value}"
                )
            if run.state in {RunState.DRAFT, RunState.PENDING_APPROVAL}:
                raise InvalidStateTransition(
                    f"cannot record milestone gate approval before operational approval for run {run_id}"
                )
            if intent != run.intent:
                raise IntentMismatch("gate approval intent does not match the run's immutable intent")
            conn.execute(
                "INSERT INTO milestone_gate_approvals (id, run_id, gate, approver, decision, bench, site, app, provider, spec_hash, plan_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record.id, record.run_id, record.gate, record.approver, record.decision,
                 intent.target.bench, intent.target.site, intent.target.app, intent.provider,
                 intent.spec_hash, intent.plan_hash, record.created_at),
            )
        return record

    def get_milestone_gate_approval(self, approval_id: str) -> MilestoneGateApprovalRecord:
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM milestone_gate_approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            raise RunNotFound(f"milestone gate approval {approval_id}")
        return self._gate_approval_from_row(row)

    def list_milestone_gate_approvals(self, run_id: str, *, gate: str | None = None) -> tuple[MilestoneGateApprovalRecord, ...]:
        """Return immutable gate approvals for one run, optionally one gate."""
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is None:
                raise RunNotFound(run_id)
            rows = conn.execute(
                "SELECT * FROM milestone_gate_approvals WHERE run_id = ?" + (" AND gate = ?" if gate else "") + " ORDER BY created_at, id",
                (run_id, gate) if gate else (run_id,),
            ).fetchall()
        return tuple(self._gate_approval_from_row(row) for row in rows)

    def start_command(self, run_id: str, *, command: str, arguments: Mapping[str, Any] | None = None) -> CommandReceipt:
        """Persist receipt admission for an actively locked running run only."""
        if not command.strip():
            raise ValueError("command must not be blank")
        empty_digest = hashlib.sha256(b"").hexdigest()
        receipt = CommandReceipt(
            str(uuid4()), run_id, command, dict(arguments or {}), "started",
            0, empty_digest, 0, empty_digest, None, _utc_now(), None,
        )
        with self._transaction() as conn:
            run = conn.execute("SELECT state, bench, site, app FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise RunNotFound(run_id)
            if RunState(run["state"]) is not RunState.RUNNING:
                raise InvalidStateTransition("commands may start only while the run is running")
            lock = conn.execute(
                "SELECT 1 FROM target_locks WHERE bench = ? AND site = ? AND app = ? AND run_id = ?",
                (run["bench"], run["site"], run["app"], run_id),
            ).fetchone()
            if lock is None:
                raise RunStoreError("a running command requires the run's active target lock")
            conn.execute(
                "INSERT INTO command_receipts (id, run_id, command, arguments_json, status, stdout_bytes, stdout_sha256, "
                "stderr_bytes, stderr_sha256, exit_code, created_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (receipt.id, receipt.run_id, receipt.command, json.dumps(receipt.arguments, sort_keys=True), receipt.status,
                 receipt.stdout_bytes, receipt.stdout_sha256, receipt.stderr_bytes, receipt.stderr_sha256,
                 receipt.exit_code, receipt.created_at, receipt.completed_at),
            )
        return receipt

    def complete_command(
        self, receipt_id: str, *, exit_code: int, stdout: str | bytes = "", stderr: str | bytes = ""
    ) -> CommandReceipt:
        """Complete a receipt without persisting raw process output."""
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise ValueError("exit_code must be an integer")
        stdout_data = _output_bytes(stdout)
        stderr_data = _output_bytes(stderr)
        return self.complete_command_summary(
            receipt_id,
            exit_code=exit_code,
            stdout_bytes=len(stdout_data),
            stdout_sha256=hashlib.sha256(stdout_data).hexdigest(),
            stderr_bytes=len(stderr_data),
            stderr_sha256=hashlib.sha256(stderr_data).hexdigest(),
        )

    def complete_command_summary(
        self,
        receipt_id: str,
        *,
        exit_code: int | None,
        stdout_bytes: int,
        stdout_sha256: str,
        stderr_bytes: int,
        stderr_sha256: str,
    ) -> CommandReceipt:
        """Finalize a receipt using pre-redacted process-output evidence.

        Execution adapters such as :class:`BenchRunner` intentionally discard
        child output after deriving its digest.  This method lets them persist
        that evidence without reintroducing a raw-output transport into the
        durable control plane.
        """
        if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
            raise ValueError("exit_code must be an integer or None")
        for label, value in (("stdout_bytes", stdout_bytes), ("stderr_bytes", stderr_bytes)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{label} must be a non-negative integer")
        for label, value in (("stdout_sha256", stdout_sha256), ("stderr_sha256", stderr_sha256)):
            if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
                raise ValueError(f"{label} must be a 64-character lower-case SHA-256 hex digest")
        status = "completed" if exit_code == 0 else "failed"
        completed_at = _utc_now()
        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE command_receipts SET status = ?, stdout_bytes = ?, stdout_sha256 = ?, stderr_bytes = ?, "
                "stderr_sha256 = ?, exit_code = ?, completed_at = ? "
                "WHERE id = ? AND status = 'started'",
                (status, stdout_bytes, stdout_sha256, stderr_bytes, stderr_sha256, exit_code, completed_at, receipt_id),
            )
            if cursor.rowcount != 1:
                raise RunStoreError(f"command receipt is missing or not pending: {receipt_id}")
            row = conn.execute("SELECT * FROM command_receipts WHERE id = ?", (receipt_id,)).fetchone()
        return self._receipt_from_row(row)

    def get_lifecycle_snapshot(self, run_id: str) -> PersistedLifecycleSnapshot | None:
        """Return the latest verified snapshot, or ``None`` before first persistence."""

        with self._transaction() as conn:
            run = self._run_from_database(conn, run_id)
            row = conn.execute(
                "SELECT * FROM lifecycle_snapshots WHERE run_id = ? ORDER BY revision DESC LIMIT 1", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return self._snapshot_from_row(row, expected_intent_hash=run_intent_digest(run.intent))

    def append_lifecycle_snapshot(self, run_id: str, snapshot: LifecycleSnapshot) -> PersistedLifecycleSnapshot:
        """Append a hash-bound lifecycle revision for an active, locked run."""

        with self._transaction() as conn:
            run = self._running_run(conn, run_id)
            expected_intent_hash = run_intent_digest(run.intent)
            if snapshot.intent_hash != expected_intent_hash:
                raise IntentMismatch("lifecycle snapshot does not bind this run's immutable intent")
            latest = self._latest_snapshot(conn, run_id, expected_intent_hash)
            if latest is not None and latest.snapshot == snapshot:
                raise InvalidStateTransition("lifecycle snapshot must advance before it can be appended")
            previous = latest.snapshot if latest else LifecycleSnapshot(expected_intent_hash)
            newly_passed = [
                state.value for state in LifecycleState
                if previous.record_for(state).status.value != "passed"
                and snapshot.record_for(state).status.value == "passed"
            ]
            if newly_passed:
                raise InvalidStateTransition("lifecycle passed states require immutable evidence records")
            revision = 1 if latest is None else latest.revision + 1
            payload = _snapshot_json(snapshot)
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            created_at = _utc_now()
            conn.execute(
                "INSERT INTO lifecycle_snapshots (run_id, revision, intent_hash, snapshot_json, snapshot_sha256, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, revision, expected_intent_hash, payload, digest, created_at),
            )
        return PersistedLifecycleSnapshot(run_id, revision, snapshot, digest, created_at)

    def record_lifecycle_evidence(self, run_id: str, kind: str, payload: Mapping[str, Any]) -> LifecycleEvidenceRecord:
        """Persist one normalized local evidence fact for a running run."""

        if not isinstance(kind, str) or not kind.strip():
            raise ValueError("evidence kind must be non-blank")
        if not isinstance(payload, Mapping):
            raise ValueError("evidence payload must be an object")
        with self._transaction() as conn:
            run = self._running_run(conn, run_id)
            return self._insert_lifecycle_evidence(conn, run.id, kind, payload)

    def get_lifecycle_evidence(self, evidence_id: str) -> LifecycleEvidenceRecord:
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM lifecycle_evidence WHERE id = ?", (evidence_id,)).fetchone()
        if row is None:
            raise RunNotFound(f"lifecycle evidence {evidence_id}")
        return self._evidence_from_row(row)

    def get_lifecycle_evidence_payload(self, evidence_id: str) -> Mapping[str, Any]:
        """Read one normalized evidence payload for a gate-specific validator."""
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM lifecycle_evidence WHERE id = ?", (evidence_id,)).fetchone()
        if row is None:
            raise RunNotFound(f"lifecycle evidence {evidence_id}")
        self._evidence_from_row(row)  # validate the retained digest and JSON
        payload = json.loads(row["payload_json"])
        if not isinstance(payload, Mapping):
            raise RunStoreError("persisted lifecycle evidence payload is not an object")
        return payload

    def get_command_receipt(self, receipt_id: str) -> CommandReceipt:
        """Read one redacted command receipt for exact recovery validation."""
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM command_receipts WHERE id = ?", (receipt_id,)).fetchone()
        if row is None:
            raise RunNotFound(f"command receipt {receipt_id}")
        return self._receipt_from_row(row)

    def advance_lifecycle_state(
        self, run_id: str, state: LifecycleState, evidence_ids: tuple[str, ...],
    ) -> PersistedLifecycleSnapshot:
        """Atomically pass one state using its exact immutable evidence kinds."""

        with self._transaction() as conn:
            run = self._running_run(conn, run_id)
            intent_hash = run_intent_digest(run.intent)
            current = self._latest_snapshot(conn, run_id, intent_hash)
            snapshot = current.snapshot if current else LifecycleSnapshot(intent_hash)
            records = [self._evidence_from_database(conn, evidence_id) for evidence_id in evidence_ids]
            if any(record.run_id != run_id for record in records):
                raise IntentMismatch("lifecycle evidence belongs to another run")
            evidence = tuple(Evidence(record.kind, record.sha256, record.reference) for record in records)
            try:
                advanced = record_pass(snapshot, state, evidence)
            except LifecycleError as error:
                raise InvalidStateTransition(str(error)) from error
            revision = 1 if current is None else current.revision + 1
            payload = _snapshot_json(advanced)
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            created_at = _utc_now()
            conn.execute(
                "INSERT INTO lifecycle_snapshots (run_id, revision, intent_hash, snapshot_json, snapshot_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, revision, intent_hash, payload, digest, created_at),
            )
        return PersistedLifecycleSnapshot(run_id, revision, advanced, digest, created_at)

    def record_lifecycle_attempt(
        self, run_id: str, state: LifecycleState, outcome: AttemptOutcome,
        *, failure_code: str | None = None, policy: RetryPolicy = RetryPolicy(),
    ) -> PersistedLifecycleSnapshot:
        """Persist one typed lifecycle attempt and its retry status."""

        with self._transaction() as conn:
            run = self._running_run(conn, run_id)
            intent_hash = run_intent_digest(run.intent)
            current = self._latest_snapshot(conn, run_id, intent_hash)
            snapshot = current.snapshot if current else LifecycleSnapshot(intent_hash)
            try:
                advanced = record_attempt(snapshot, state, outcome, failure_code=failure_code, policy=policy)
            except LifecycleError as error:
                raise InvalidStateTransition(str(error)) from error
            return self._append_snapshot_transaction(conn, run_id, intent_hash, current, advanced)

    def reconcile_interrupted_migration(self, run_id: str, *, evidence_id: str) -> PersistedLifecycleSnapshot:
        """Make an interrupted migration retryable only with typed reconciliation evidence."""

        with self._transaction() as conn:
            run = self._running_run(conn, run_id)
            evidence = self._evidence_from_database(conn, evidence_id)
            if evidence.run_id != run_id or evidence.kind != "migration_reconciliation":
                raise IntentMismatch("migration reconciliation evidence does not belong to this run")
            intent_hash = run_intent_digest(run.intent)
            current = self._latest_snapshot(conn, run_id, intent_hash)
            snapshot = current.snapshot if current else LifecycleSnapshot(intent_hash)
            try:
                advanced = reconcile_interrupted(snapshot, LifecycleState.MIGRATION_APPLIED)
            except LifecycleError as error:
                raise InvalidStateTransition(str(error)) from error
            return self._append_snapshot_transaction(conn, run_id, intent_hash, current, advanced)

    def invalidate_lifecycle_from(
        self, run_id: str, earliest_state: LifecycleState, *, evidence_id: str,
    ) -> PersistedLifecycleSnapshot:
        """Persist explicit invalidation of an approved run's affected suffix."""

        with self._transaction() as conn:
            run = self._running_run(conn, run_id)
            evidence = self._evidence_from_database(conn, evidence_id)
            if evidence.run_id != run_id or evidence.kind != "spec_invalidation":
                raise IntentMismatch("spec invalidation evidence does not belong to this run")
            intent_hash = run_intent_digest(run.intent)
            current = self._latest_snapshot(conn, run_id, intent_hash)
            snapshot = current.snapshot if current else LifecycleSnapshot(intent_hash)
            advanced = apply_invalidation(snapshot, earliest_state)
            return self._append_snapshot_transaction(conn, run_id, intent_hash, current, advanced)

    def _append_snapshot_transaction(
        self, conn: sqlite3.Connection, run_id: str, intent_hash: str,
        latest: PersistedLifecycleSnapshot | None, snapshot: LifecycleSnapshot,
    ) -> PersistedLifecycleSnapshot:
        if snapshot.intent_hash != intent_hash or (latest is not None and latest.snapshot == snapshot):
            raise InvalidStateTransition("lifecycle snapshot must advance before it can be appended")
        payload = _snapshot_json(snapshot)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        created_at = _utc_now()
        revision = 1 if latest is None else latest.revision + 1
        conn.execute(
            "INSERT INTO lifecycle_snapshots (run_id, revision, intent_hash, snapshot_json, snapshot_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, revision, intent_hash, payload, digest, created_at),
        )
        return PersistedLifecycleSnapshot(run_id, revision, snapshot, digest, created_at)

    def record_verification_report(
        self, run_id: str, program: VerificationProgram, report: VerificationRunReport,
    ) -> VerificationReportRecord:
        """Persist a normalized report only when it exactly binds the approved spec/program."""

        with self._transaction() as conn:
            run = self._running_run(conn, run_id)
            return self._insert_verification_report(conn, run, program, report)

    def get_verification_report(self, report_id: str) -> VerificationReportRecord:
        """Read report identity after checking its retained normalized bytes and intent binding."""

        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM verification_reports WHERE id = ?", (report_id,)).fetchone()
            if row is None:
                raise RunNotFound(f"verification report {report_id}")
            run = self._run_from_database(conn, row["run_id"])
        return self._verification_report_from_row(row, expected_spec_hash=run.intent.spec_hash)

    def bridge_postrun_backend_verification(self, run_id: str, report_id: str) -> LifecycleEvidenceRecord:
        """Bind an existing passing report to VER-GATE after successful closure.

        The report remains normalized and immutable in ``verification_reports``;
        this append-only bridge supplies the lifecycle evidence reference needed
        by governance without reopening or mutating the completed run.
        """
        with self._transaction() as conn:
            run = self._run_from_database(conn, run_id)
            if run.state is not RunState.SUCCEEDED:
                raise InvalidStateTransition("post-run backend evidence requires a succeeded run")
            row = conn.execute("SELECT * FROM verification_reports WHERE id = ? AND run_id = ?", (report_id, run_id)).fetchone()
            if row is None:
                raise RunNotFound(f"verification report {report_id}")
            if row["spec_hash"] != run.intent.spec_hash:
                raise IntentMismatch("verification report does not bind the run's approved spec")
            try:
                report = json.loads(row["report_json"])
            except json.JSONDecodeError as error:
                raise RunStoreError("verification report is not valid normalized JSON") from error
            results = report.get("results") if isinstance(report, Mapping) else None
            if (not isinstance(results, list) or not results
                    or not all(isinstance(item, Mapping) for item in results)
                    or any(item.get("status") != "passed" for item in results)):
                raise InvalidStateTransition("post-run backend evidence requires an all-pass normalized report")
            return self._insert_lifecycle_evidence(conn, run.id, "backend_verification", {
                "report_reference": f"run:{run_id}:verification:{report_id}",
                "report_sha256": row["report_sha256"],
                "case_count": len(results),
            })

    def bridge_postrun_pi_compatibility(
        self, run_id: str, *, node_runtime: str, pi_version: str,
        model: str, isolated_probe_passed: bool, evidence_sha256: str,
    ) -> LifecycleEvidenceRecord:
        """Bind a supported, tool-disabled Pi probe to a completed run."""
        if not isolated_probe_passed:
            raise InvalidStateTransition("Pi compatibility evidence requires a passing isolated probe")
        if not isinstance(evidence_sha256, str) or len(evidence_sha256) != 64:
            raise ValueError("Pi compatibility evidence requires a SHA-256 digest")
        try:
            major = int(node_runtime.split(".", 1)[0].lstrip("v"))
        except (AttributeError, ValueError):
            raise ValueError("Pi compatibility evidence requires a parseable Node runtime")
        if major < 20 or major >= 23:
            raise ValueError("Pi compatibility evidence requires Node 20, 21, or 22")
        with self._transaction() as conn:
            run = self._run_from_database(conn, run_id)
            if run.state is not RunState.SUCCEEDED:
                raise InvalidStateTransition("post-run Pi evidence requires a succeeded run")
            return self._insert_lifecycle_evidence(conn, run.id, "pi_compatibility", {
                "node_runtime": node_runtime, "pi_version": pi_version, "model": model,
                "tools_disabled": True, "extensions_disabled": True, "skills_disabled": True,
                "session_persistence_disabled": True, "evidence_sha256": evidence_sha256,
            })

    def bridge_postrun_governance(
        self, run_id: str, *, kind: str, evidence_refs: Mapping[str, str],
    ) -> LifecycleEvidenceRecord:
        """Create a strict post-success governance evidence fact for a succeeded run.

        Supported ``kind`` values and their exact same-run prerequisites:

        * ``compiler_golden`` (CMP-GATE): ``compilation`` and ``artifact_audit``
        * ``controlled_execution`` (BEX-GATE): ``environment_preflight`` and one
          of ``generated_artifact_deployment`` or ``migration_receipt``
        * ``safety_validation`` (VAL-GATE): ``artifact_audit`` and one of
          ``destructive_no_mutation`` or ``modification_destructive_no_mutation``

        Each entry in ``evidence_refs`` maps an expected prerequisite kind to a
        durable reference of the form ``run:<run_id>:evidence:<id>``.  The
        method validates that the reference resolves to evidence in this run,
        that the recorded kind matches the declared key, and that the full set
        of prerequisites is present before creating the new governance evidence
        record.  Failed, cancelled, or still-running runs are rejected.
        """
        schemas: Mapping[str, frozenset[str]] = {
            "compiler_golden": frozenset({"compilation", "artifact_audit"}),
            "controlled_execution": frozenset({"environment_preflight", "generated_artifact_deployment", "migration_receipt"}),
            "safety_validation": frozenset({"artifact_audit", "destructive_no_mutation", "modification_destructive_no_mutation"}),
        }
        if kind not in schemas:
            raise ValueError(f"unsupported governance evidence kind: {kind}")
        if not isinstance(evidence_refs, Mapping):
            raise ValueError("evidence_refs must be a mapping from expected kind to durable reference")

        required_base: frozenset[str] = {
            "compiler_golden": frozenset({"compilation", "artifact_audit"}),
            "controlled_execution": frozenset({"environment_preflight"}),
            "safety_validation": frozenset({"artifact_audit"}),
        }[kind]
        alternatives: frozenset[str] | None = {
            "controlled_execution": frozenset({"generated_artifact_deployment", "migration_receipt"}),
            "safety_validation": frozenset({"destructive_no_mutation", "modification_destructive_no_mutation"}),
        }.get(kind)

        with self._transaction() as conn:
            run = self._run_from_database(conn, run_id)
            if run.state is not RunState.SUCCEEDED:
                raise InvalidStateTransition("governance evidence requires a succeeded run")

            resolved_kinds: set[str] = set()
            resolved_refs: dict[str, str] = {}
            for declared_kind, reference in evidence_refs.items():
                if not isinstance(declared_kind, str) or not declared_kind.strip():
                    raise ValueError("evidence_refs keys must be non-blank strings")
                if not isinstance(reference, str) or not reference.strip():
                    raise ValueError("evidence_refs values must be non-blank durable references")
                match = re.fullmatch(r"run:(?P<run>[^:]+):evidence:(?P<id>[0-9a-f-]+)", reference)
                if match is None or match.group("run") != run_id:
                    raise IntentMismatch(f"{declared_kind} evidence reference is not bound to this run")
                row = conn.execute("SELECT * FROM lifecycle_evidence WHERE id = ?", (match.group("id"),)).fetchone()
                if row is None:
                    raise RunNotFound(f"{declared_kind} evidence reference does not exist")
                if row["run_id"] != run_id:
                    raise IntentMismatch(f"{declared_kind} evidence reference belongs to another run")
                actual_kind = row["kind"]
                if actual_kind != declared_kind:
                    raise IntentMismatch(f"{declared_kind} evidence reference resolves to kind {actual_kind}")
                if actual_kind not in schemas[kind]:
                    raise InvalidStateTransition(f"{actual_kind} is not a valid prerequisite for {kind}")
                if actual_kind in resolved_kinds:
                    raise InvalidStateTransition(f"duplicate prerequisite kind: {actual_kind}")
                resolved_kinds.add(actual_kind)
                resolved_refs[actual_kind] = reference

            missing = required_base - resolved_kinds
            if missing:
                raise InvalidStateTransition(f"{kind} missing required prerequisites: {sorted(missing)}")
            if alternatives is not None and not (alternatives & resolved_kinds):
                raise InvalidStateTransition(f"{kind} requires one of: {sorted(alternatives)}")

            return self._insert_lifecycle_evidence(conn, run.id, kind, {
                "prerequisite_references": resolved_refs,
            })

    def record_backend_verification(
        self, run_id: str, program: VerificationProgram, report: VerificationRunReport,
    ) -> tuple[VerificationReportRecord, PersistedLifecycleSnapshot]:
        """Atomically bind a passing report to ``BACKEND_VERIFIED`` lifecycle evidence."""

        if not report.passed:
            raise InvalidStateTransition("a failing verification report cannot pass backend verification")
        with self._transaction() as conn:
            run = self._running_run(conn, run_id)
            report_record = self._insert_verification_report(conn, run, program, report)
            intent_hash = run_intent_digest(run.intent)
            current = self._latest_snapshot(conn, run_id, intent_hash)
            snapshot = current.snapshot if current is not None else LifecycleSnapshot(intent_hash)
            try:
                advanced = record_pass(
                    snapshot,
                    LifecycleState.BACKEND_VERIFIED,
                    (Evidence("backend_verification", report_record.sha256, report_record.reference),),
                )
            except LifecycleError as error:
                raise InvalidStateTransition(str(error)) from error
            revision = 1 if current is None else current.revision + 1
            payload = _snapshot_json(advanced)
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            created_at = _utc_now()
            conn.execute(
                "INSERT INTO lifecycle_snapshots (run_id, revision, intent_hash, snapshot_json, snapshot_sha256, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, revision, intent_hash, payload, digest, created_at),
            )
        return report_record, PersistedLifecycleSnapshot(run_id, revision, advanced, digest, created_at)

    def _insert_verification_report(
        self, conn: sqlite3.Connection, run: RunRecord, program: VerificationProgram, report: VerificationRunReport,
    ) -> VerificationReportRecord:
        if program.spec_hash != run.intent.spec_hash:
            raise IntentMismatch("verification program does not bind the run's approved spec")
        if report.program_hash != program.template_hash:
            raise IntentMismatch("verification report does not bind the verification program")
        expected_ids = tuple(case.id for case in program.cases)
        actual_ids = tuple(result.case_id for result in report.results)
        if actual_ids != expected_ids or len(set(actual_ids)) != len(actual_ids):
            raise IntentMismatch("verification report cases do not exactly match the verification program")
        for case, result in zip(program.cases, report.results, strict=True):
            if result.expected != case.expected or result.status not in {"passed", "failed", "skipped"}:
                raise IntentMismatch("verification report result is not normalized for its program case")
            if result.status == "passed" and result.observed != case.expected:
                raise IntentMismatch("passing verification result does not match expected outcome")
        payload = _verification_report_json(program, report)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        record = VerificationReportRecord(
            str(uuid4()), run.id, program.spec_hash, program.template_hash, digest, _utc_now(),
        )
        conn.execute(
            "INSERT INTO verification_reports (id, run_id, spec_hash, template_hash, report_json, report_sha256, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (record.id, record.run_id, record.spec_hash, record.template_hash, payload, record.sha256, record.created_at),
        )
        return record

    def _running_run(self, conn: sqlite3.Connection, run_id: str) -> RunRecord:
        run = self._run_from_database(conn, run_id)
        if run.state is not RunState.RUNNING:
            raise InvalidStateTransition("lifecycle and verification evidence may persist only for a running run")
        return run

    def _run_from_database(self, conn: sqlite3.Connection, run_id: str) -> RunRecord:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFound(run_id)
        return self._run_from_row(row)

    def _latest_snapshot(
        self, conn: sqlite3.Connection, run_id: str, expected_intent_hash: str,
    ) -> PersistedLifecycleSnapshot | None:
        row = conn.execute(
            "SELECT * FROM lifecycle_snapshots WHERE run_id = ? ORDER BY revision DESC LIMIT 1", (run_id,)
        ).fetchone()
        return None if row is None else self._snapshot_from_row(row, expected_intent_hash=expected_intent_hash)

    def _insert_lifecycle_evidence(
        self, conn: sqlite3.Connection, run_id: str, kind: str, payload: Mapping[str, Any],
    ) -> LifecycleEvidenceRecord:
        rendered = _canonical_json(dict(payload))
        digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        record = LifecycleEvidenceRecord(str(uuid4()), run_id, kind, digest, _utc_now())
        conn.execute(
            "INSERT INTO lifecycle_evidence (id, run_id, kind, payload_json, payload_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (record.id, record.run_id, record.kind, rendered, record.sha256, record.created_at),
        )
        return record

    def _evidence_from_database(self, conn: sqlite3.Connection, evidence_id: str) -> LifecycleEvidenceRecord:
        row = conn.execute("SELECT * FROM lifecycle_evidence WHERE id = ?", (evidence_id,)).fetchone()
        if row is None:
            raise RunNotFound(f"lifecycle evidence {evidence_id}")
        return self._evidence_from_row(row)

    @staticmethod
    def _has_matching_approval(conn: sqlite3.Connection, run: sqlite3.Row) -> bool:
        """Return whether the latest exact-intent decision is approval.

        Rejections are immutable audit facts and must revoke execution even if
        an older approval exists; otherwise an operator could append a denial
        and still transition the run through a stale approval.
        """
        row = conn.execute(
            "SELECT decision FROM approvals WHERE run_id = ? "
            "AND bench = ? AND site = ? AND app = ? AND provider = ? AND spec_hash = ? AND plan_hash = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (
                run["id"], run["bench"], run["site"], run["app"], run["provider"],
                run["spec_hash"], run["plan_hash"],
            ),
        ).fetchone()
        return row is not None and row["decision"] == "approved"

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> RunRecord:
        target = LockTarget(row["bench"], row["site"], row["app"])
        if not all(row[name] for name in ("provider", "spec_hash", "plan_hash")):
            raise RunStoreError(f"legacy run {row['id']} has no immutable intent binding")
        intent = RunIntent(target, row["provider"], row["spec_hash"], row["plan_hash"])
        return RunRecord(row["id"], target, RunState(row["state"]), row["created_at"], row["updated_at"], json.loads(row["metadata_json"]), intent)

    @staticmethod
    def _receipt_from_row(row: sqlite3.Row) -> CommandReceipt:
        return CommandReceipt(
            row["id"], row["run_id"], row["command"], json.loads(row["arguments_json"]), row["status"],
            row["stdout_bytes"], row["stdout_sha256"], row["stderr_bytes"], row["stderr_sha256"],
            row["exit_code"], row["created_at"], row["completed_at"],
        )

    @staticmethod
    def _approval_from_row(row: sqlite3.Row) -> ApprovalRecord:
        target = LockTarget(row["bench"], row["site"], row["app"])
        if not all(row[name] for name in ("provider", "spec_hash", "plan_hash")):
            raise RunStoreError(f"legacy approval {row['id']} has no immutable intent binding")
        intent = RunIntent(target, row["provider"], row["spec_hash"], row["plan_hash"])
        return ApprovalRecord(
            row["id"], row["run_id"], row["approver"], row["decision"], row["rationale"], row["created_at"], intent
        )

    @staticmethod
    def _gate_approval_from_row(row: sqlite3.Row) -> MilestoneGateApprovalRecord:
        target = LockTarget(row["bench"], row["site"], row["app"])
        if not all(row[name] for name in ("provider", "spec_hash", "plan_hash")):
            raise RunStoreError(f"gate approval {row['id']} has no immutable intent binding")
        intent = RunIntent(target, row["provider"], row["spec_hash"], row["plan_hash"])
        return MilestoneGateApprovalRecord(
            row["id"], row["run_id"], row["gate"], row["approver"], row["decision"], row["created_at"], intent,
        )

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row, *, expected_intent_hash: str) -> PersistedLifecycleSnapshot:
        if row["intent_hash"] != expected_intent_hash:
            raise IntentMismatch("persisted lifecycle snapshot does not bind the run's immutable intent")
        payload = row["snapshot_json"]
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if digest != row["snapshot_sha256"]:
            raise RunStoreError("persisted lifecycle snapshot digest does not match retained bytes")
        snapshot = _snapshot_from_json(payload)
        if snapshot.intent_hash != expected_intent_hash:
            raise IntentMismatch("persisted lifecycle snapshot payload does not bind the run's immutable intent")
        return PersistedLifecycleSnapshot(row["run_id"], row["revision"], snapshot, row["snapshot_sha256"], row["created_at"])

    @staticmethod
    def _verification_report_from_row(row: sqlite3.Row, *, expected_spec_hash: str) -> VerificationReportRecord:
        if row["spec_hash"] != expected_spec_hash:
            raise IntentMismatch("persisted verification report does not bind the run's approved spec")
        payload = row["report_json"]
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if digest != row["report_sha256"]:
            raise RunStoreError("persisted verification report digest does not match retained bytes")
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as error:
            raise RunStoreError("persisted verification report is not valid JSON") from error
        if not isinstance(value, Mapping) or value.get("spec_hash") != row["spec_hash"] or value.get("template_hash") != row["template_hash"]:
            raise RunStoreError("persisted verification report bytes do not match report identity")
        return VerificationReportRecord(
            row["id"], row["run_id"], row["spec_hash"], row["template_hash"], row["report_sha256"], row["created_at"],
        )

    @staticmethod
    def _evidence_from_row(row: sqlite3.Row) -> LifecycleEvidenceRecord:
        digest = hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest()
        if digest != row["payload_sha256"]:
            raise RunStoreError("persisted lifecycle evidence digest does not match retained bytes")
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError as error:
            raise RunStoreError("persisted lifecycle evidence is not valid JSON") from error
        if not isinstance(payload, Mapping):
            raise RunStoreError("persisted lifecycle evidence payload is not an object")
        return LifecycleEvidenceRecord(row["id"], row["run_id"], row["kind"], row["payload_sha256"], row["created_at"])


def run_intent_digest(intent: RunIntent) -> str:
    """Hash every immutable run-intent dimension, including its target identity."""

    value = {
        "target": {"bench": intent.target.bench, "site": intent.target.site, "app": intent.target.app},
        "provider": intent.provider,
        "spec_hash": intent.spec_hash,
        "plan_hash": intent.plan_hash,
    }
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _snapshot_json(snapshot: LifecycleSnapshot) -> str:
    return _canonical_json({
        "intent_hash": snapshot.intent_hash,
        "records": [
            {
                "state": record.state.value,
                "status": record.status.value,
                "evidence": [
                    {"kind": evidence.kind, "digest": evidence.digest, "reference": evidence.reference}
                    for evidence in record.evidence
                ],
                "attempts": [
                    {"state": attempt.state.value, "number": attempt.number, "outcome": attempt.outcome.value,
                     "failure_code": attempt.failure_code}
                    for attempt in record.attempts
                ],
            }
            for record in snapshot.records
        ],
    })


def _snapshot_from_json(payload: str) -> LifecycleSnapshot:
    try:
        value = json.loads(payload)
        if not isinstance(value, Mapping) or not isinstance(value.get("records"), list):
            raise ValueError("snapshot must be an object with records")
        from .lifecycle_contract import Attempt, AttemptOutcome, StateRecord, StateStatus
        records = []
        for raw_record in value["records"]:
            if not isinstance(raw_record, Mapping) or not isinstance(raw_record.get("evidence"), list) or not isinstance(raw_record.get("attempts"), list):
                raise ValueError("snapshot record is malformed")
            if not all(isinstance(item, Mapping) for item in raw_record["evidence"] + raw_record["attempts"]):
                raise ValueError("snapshot evidence and attempts must be objects")
            records.append(StateRecord(
                LifecycleState(raw_record["state"]), StateStatus(raw_record["status"]),
                tuple(Evidence(item["kind"], item["digest"], item["reference"]) for item in raw_record["evidence"]),
                tuple(Attempt(LifecycleState(item["state"]), item["number"], AttemptOutcome(item["outcome"]), item.get("failure_code")) for item in raw_record["attempts"]),
            ))
        return LifecycleSnapshot(value["intent_hash"], tuple(records))
    except (KeyError, TypeError, ValueError, LifecycleError) as error:
        raise RunStoreError("persisted lifecycle snapshot is malformed") from error


def _verification_report_json(program: VerificationProgram, report: VerificationRunReport) -> str:
    return _canonical_json({
        "spec_hash": program.spec_hash,
        "template_hash": program.template_hash,
        "results": [
            {"case_id": result.case_id, "expected": result.expected, "observed": result.observed,
             "status": result.status, "code": result.code}
            for result in report.results
        ],
    })


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
