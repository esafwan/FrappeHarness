"""Pure typed staged human-in-the-loop state machine.

Stages advance in fixed order: PLAN → DOCTYPES → PERMISSIONS → UI_TESTS →
RELEASE.  Every transition requires an explicit approval record created by
Python.  The module has no model, tool, network, filesystem, or database
dependency; it stores only normalized stage identifiers, status, reason, and a
digest.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

_SCHEMA_VERSION = 1
_MAX_REASON = 256
_MAX_APPROVER_ID = 128

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class Stage(str, Enum):
    PLAN = "PLAN"
    DOCTYPES = "DOCTYPES"
    PERMISSIONS = "PERMISSIONS"
    UI_TESTS = "UI_TESTS"
    RELEASE = "RELEASE"


class StageStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"


class StageFlowError(ValueError):
    """Raised when a stage transition is invalid or a record is malformed."""


_STAGE_ORDER: tuple[Stage, ...] = (
    Stage.PLAN,
    Stage.DOCTYPES,
    Stage.PERMISSIONS,
    Stage.UI_TESTS,
    Stage.RELEASE,
)

_STAGE_INDEX: dict[Stage, int] = {stage: index for index, stage in enumerate(_STAGE_ORDER)}


def _identifier(value: object, path: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise StageFlowError(f"{path} must be a bounded identifier")
    return value


def _text(value: object, path: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise StageFlowError(f"{path} must be non-blank text of at most {limit} characters")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise StageFlowError(f"{path} contains forbidden control characters")
    return value


def _digest_value(value: object, path: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise StageFlowError(f"{path} must be a lowercase SHA-256 digest")
    return value


# ---------------------------------------------------------------------------
# Approval record
# ---------------------------------------------------------------------------


def _canonical_approval(approval: StageApproval) -> str:
    data = {
        "schema_version": approval.schema_version,
        "stage": approval.stage.value,
        "approver_id": approval.approver_id,
        "reason": approval.reason,
    }
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def approval_digest(approval: StageApproval) -> str:
    """Return the deterministic digest of an approval record."""
    return hashlib.sha256(_canonical_approval(approval).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StageApproval:
    """Explicit Python-supplied approval for completing one stage."""

    stage: Stage
    approver_id: str
    reason: str
    approval_digest: str
    schema_version: int = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise StageFlowError("unsupported stage approval schema_version")
        if not isinstance(self.stage, Stage):
            raise StageFlowError("stage must be a Stage")
        _identifier(self.approver_id, "approver_id")
        _text(self.reason, "reason", _MAX_REASON)
        _digest_value(self.approval_digest, "approval_digest")
        if self.approval_digest != approval_digest(self):
            raise StageFlowError("approval_digest does not match approval contents")


def build_stage_approval(stage: Stage, approver_id: str, reason: str) -> StageApproval:
    """Create a stage approval with its digest calculated from contents."""
    provisional = object.__new__(StageApproval)
    object.__setattr__(provisional, "schema_version", _SCHEMA_VERSION)
    object.__setattr__(provisional, "stage", stage)
    object.__setattr__(provisional, "approver_id", approver_id)
    object.__setattr__(provisional, "reason", reason)
    object.__setattr__(provisional, "approval_digest", "0" * 64)
    digest = approval_digest(provisional)
    return StageApproval(
        stage=stage,
        approver_id=approver_id,
        reason=reason,
        approval_digest=digest,
    )


# Backwards-compatible alias used by the existing CLI surface.
build_approval = build_stage_approval


# ---------------------------------------------------------------------------
# Stage record
# ---------------------------------------------------------------------------


def _canonical_record(record: StageRecord, approval_digest: str | None = None) -> str:
    data: dict[str, Any] = {
        "schema_version": record.schema_version,
        "stage": record.stage.value,
        "status": record.status.value,
        "reason": record.reason,
    }
    if approval_digest is not None:
        data["approval_digest"] = approval_digest
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def stage_record_digest(record: StageRecord, approval_digest: str | None = None) -> str:
    """Return the deterministic digest of a stage record.

    When ``approval_digest`` is supplied, the returned digest binds the record
    to that approval.  Callers that only need a digest over the stored fields
    may omit it.
    """
    return hashlib.sha256(_canonical_record(record, approval_digest).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StageRecord:
    """One durable, normalized stage transition outcome.

    Stores only the stage identifier, status, reason, and a digest.  When the
    record is produced by :func:`build_stage_record`, the digest binds the
    approval that authorized the transition.
    """

    stage: Stage
    status: StageStatus
    reason: str
    stage_digest: str
    schema_version: int = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise StageFlowError("unsupported stage record schema_version")
        if not isinstance(self.stage, Stage):
            raise StageFlowError("stage must be a Stage")
        if not isinstance(self.status, StageStatus):
            raise StageFlowError("status must be a StageStatus")
        _text(self.reason, "reason", _MAX_REASON)
        _digest_value(self.stage_digest, "stage_digest")

    def to_record(self) -> dict[str, Any]:
        """Normalized JSON-shaped representation of the stored fields only."""
        return {
            "schema_version": self.schema_version,
            "stage": self.stage.value,
            "status": self.status.value,
            "reason": self.reason,
            "stage_digest": self.stage_digest,
        }


def build_stage_record(
    stage: Stage,
    status: StageStatus,
    reason: str,
    approval: StageApproval | None = None,
) -> StageRecord:
    """Create a stage record with its digest calculated from contents.

    When ``approval`` is supplied, the record's digest binds the approval so
    that the transition cannot later be attributed to a different approval.
    """
    if approval is not None and approval.stage is not stage:
        raise StageFlowError("approval stage must match record stage")
    provisional = object.__new__(StageRecord)
    object.__setattr__(provisional, "schema_version", _SCHEMA_VERSION)
    object.__setattr__(provisional, "stage", stage)
    object.__setattr__(provisional, "status", status)
    object.__setattr__(provisional, "reason", reason)
    object.__setattr__(provisional, "stage_digest", "0" * 64)
    bound_approval_digest = approval.approval_digest if approval is not None else None
    digest = stage_record_digest(provisional, approval_digest=bound_approval_digest)
    return StageRecord(stage=stage, status=status, reason=reason, stage_digest=digest)


def verify_stage_record(record: StageRecord, approval: StageApproval) -> bool:
    """Verify that ``record`` binds ``approval``.

    Returns ``True`` only when the record's digest matches the approval-bound
    digest for its stored fields.
    """
    return record.stage_digest == stage_record_digest(record, approval_digest=approval.approval_digest)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


def _validate_stage_order(records: tuple[StageRecord, ...]) -> None:
    """Ensure records follow the fixed stage order without gaps or repeats.

    Completed stages must be contiguous from the start.  Rejected records are
    allowed only for the current stage (the stage immediately after the last
    completed one), so a rejected stage can be retried with a later completion.
    """
    completed_seen: set[Stage] = set()
    last_completed_index = -1
    for record in records:
        stage_index = _STAGE_INDEX[record.stage]
        if stage_index > last_completed_index + 1:
            raise StageFlowError(
                f"stage {record.stage.value} is out of order; expected {_STAGE_ORDER[last_completed_index + 1].value}"
            )
        if record.status is StageStatus.COMPLETED:
            if record.stage in completed_seen:
                raise StageFlowError(f"duplicate completed stage: {record.stage.value}")
            completed_seen.add(record.stage)
            last_completed_index = stage_index
        elif stage_index != last_completed_index + 1:
            raise StageFlowError(
                f"rejected stage {record.stage.value} is not the current stage"
            )


class StageFlow:
    """Deterministic staged human-in-the-loop state machine.

    The machine is initialized from an ordered tuple of stage records.  New
    transitions append a completed record for the current stage and advance to
    the next stage.  No transition is possible without a valid Python-supplied
    approval record, and skipped or out-of-order stages are rejected.
    """

    def __init__(self, records: tuple[StageRecord, ...] = ()) -> None:
        if not isinstance(records, tuple) or any(not isinstance(record, StageRecord) for record in records):
            raise StageFlowError("records must be an immutable tuple of StageRecord")
        _validate_stage_order(records)
        self._records = records

    @property
    def records(self) -> tuple[StageRecord, ...]:
        return self._records

    @property
    def current_stage(self) -> Stage:
        """Return the stage currently in progress."""
        completed = sum(1 for record in self._records if record.status is StageStatus.COMPLETED)
        if completed >= len(_STAGE_ORDER):
            return Stage.RELEASE
        return _STAGE_ORDER[completed]

    @property
    def current(self) -> Stage | None:
        """CLI-compatible view of the in-progress stage (``None`` when complete)."""
        return None if self.is_complete else self.current_stage

    @property
    def completed_stages(self) -> tuple[Stage, ...]:
        return tuple(
            record.stage for record in self._records if record.status is StageStatus.COMPLETED
        )

    @property
    def is_complete(self) -> bool:
        return len(self.completed_stages) == len(_STAGE_ORDER)

    # CLI-compatible aliases.
    complete = property(lambda self: self.is_complete)
    digest = property(lambda self: self.flow_digest())

    def transition(self, approval: StageApproval) -> StageFlow:
        """Advance one stage using an explicit Python-supplied approval.

        The approval must name the stage currently in progress.  On success, a
        new flow is returned with a completed record for the approved stage.
        The completed record is available as the last item of ``records``.
        """
        if not isinstance(approval, StageApproval):
            raise StageFlowError("approval must be a StageApproval")
        if self.is_complete:
            raise StageFlowError("flow is already complete")
        if approval.stage is not self.current_stage:
            raise StageFlowError(
                f"approval targets {approval.stage.value} but current stage is {self.current_stage.value}"
            )
        record = build_stage_record(
            stage=approval.stage,
            status=StageStatus.COMPLETED,
            reason=approval.reason,
            approval=approval,
        )
        return StageFlow(self._records + (record,))

    # CLI-compatible alias for the explicit approval transition.
    approve = transition

    def reject(self, stage: Stage, reason: str) -> StageFlow:
        """Record a rejected transition attempt for the current stage.

        The rejected record does not advance the machine.  It may only be
        recorded for the current stage and must not skip stages.
        """
        if not isinstance(stage, Stage):
            raise StageFlowError("stage must be a Stage")
        if self.is_complete:
            raise StageFlowError("flow is already complete")
        if stage is not self.current_stage:
            raise StageFlowError(
                f"rejection targets {stage.value} but current stage is {self.current_stage.value}"
            )
        record = build_stage_record(stage=stage, status=StageStatus.REJECTED, reason=reason)
        return StageFlow(self._records + (record,))

    def flow_digest(self) -> str:
        """Return a digest over the entire ordered record sequence."""
        canonical = json.dumps(
            [record.to_record() for record in self._records],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_record(self) -> dict[str, Any]:
        """Normalized JSON-shaped representation of the flow state."""
        return {
            "schema_version": _SCHEMA_VERSION,
            "current_stage": self.current_stage.value,
            "completed_stages": [stage.value for stage in self.completed_stages],
            "is_complete": self.is_complete,
            "records": [record.to_record() for record in self._records],
            "flow_digest": self.flow_digest(),
        }


def parse_stage_flow(raw: object) -> StageFlow:
    """Reconstruct a flow from a normalized JSON-shaped record sequence.

    Accepts either the minimal ``{schema_version, records}`` form or the full
    normalized state produced by :meth:`StageFlow.to_record`.  When derived
    fields are present, they are verified against the reconstructed records.
    Fails closed on unknown fields, malformed records, or out-of-order stages.
    """
    if not isinstance(raw, Mapping) or any(not isinstance(key, str) for key in raw):
        raise StageFlowError("stage flow must be an object with string keys")
    allowed_keys = {
        "schema_version", "records", "current_stage", "completed_stages", "is_complete", "flow_digest"
    }
    unknown = set(raw) - allowed_keys
    if unknown:
        raise StageFlowError(f"stage flow has unknown fields: {', '.join(sorted(unknown))}")
    if raw.get("schema_version") != _SCHEMA_VERSION:
        raise StageFlowError("stage flow schema_version must be 1")
    records_raw = raw.get("records")
    if not isinstance(records_raw, list):
        raise StageFlowError("stage flow.records must be an array")
    records: list[StageRecord] = []
    for index, item in enumerate(records_raw):
        if not isinstance(item, Mapping):
            raise StageFlowError(f"records[{index}] must be an object")
        if set(item) != {"schema_version", "stage", "status", "reason", "stage_digest"}:
            raise StageFlowError(f"records[{index}] has unknown or missing fields")
        try:
            stage = Stage(item["stage"])
            status = StageStatus(item["status"])
        except (TypeError, ValueError) as exc:
            raise StageFlowError(f"records[{index}] has an invalid stage or status") from exc
        records.append(
            StageRecord(
                stage=stage,
                status=status,
                reason=item["reason"],
                stage_digest=item["stage_digest"],
                schema_version=item["schema_version"],
            )
        )
    flow = StageFlow(tuple(records))
    if "current_stage" in raw:
        try:
            current = Stage(raw["current_stage"])
        except (TypeError, ValueError) as exc:
            raise StageFlowError("current_stage is not a Stage") from exc
        if current is not flow.current_stage:
            raise StageFlowError("current_stage does not match records")
    if "completed_stages" in raw:
        completed_raw = raw["completed_stages"]
        if not isinstance(completed_raw, list):
            raise StageFlowError("completed_stages must be an array")
        try:
            expected = [stage.value for stage in flow.completed_stages]
        except (TypeError, ValueError) as exc:
            raise StageFlowError("completed_stages contains an invalid stage") from exc
        if completed_raw != expected:
            raise StageFlowError("completed_stages does not match records")
    if "is_complete" in raw and raw["is_complete"] != flow.is_complete:
        raise StageFlowError("is_complete does not match records")
    if "flow_digest" in raw and raw["flow_digest"] != flow.flow_digest():
        raise StageFlowError("flow_digest does not match records")
    return flow
