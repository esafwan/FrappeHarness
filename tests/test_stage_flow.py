import dataclasses
import hashlib

import pytest

from frappe_harness.stage_flow import (
    Stage,
    StageApproval,
    StageFlow,
    StageFlowError,
    StageRecord,
    StageStatus,
    approval_digest,
    build_stage_approval,
    build_stage_record,
    parse_stage_flow,
    stage_record_digest,
    verify_stage_record,
)


def _approval(stage: Stage, approver: str = "operator", reason: str = "approved") -> StageApproval:
    return build_stage_approval(stage, approver, reason)


def test_initial_flow_starts_with_plan_in_progress():
    flow = StageFlow()
    assert flow.current_stage is Stage.PLAN
    assert flow.completed_stages == ()
    assert not flow.is_complete
    assert flow.records == ()


def test_approval_advances_through_all_stages_in_order():
    flow = StageFlow()
    for stage in (Stage.PLAN, Stage.DOCTYPES, Stage.PERMISSIONS, Stage.UI_TESTS, Stage.RELEASE):
        flow = flow.transition(_approval(stage))
        assert flow.current_stage is (
            Stage.RELEASE if stage is Stage.RELEASE else _next_stage(stage)
        )
    assert flow.is_complete
    assert flow.completed_stages == (
        Stage.PLAN,
        Stage.DOCTYPES,
        Stage.PERMISSIONS,
        Stage.UI_TESTS,
        Stage.RELEASE,
    )
    assert all(record.status is StageStatus.COMPLETED for record in flow.records)


def _next_stage(stage: Stage) -> Stage:
    order = [Stage.PLAN, Stage.DOCTYPES, Stage.PERMISSIONS, Stage.UI_TESTS, Stage.RELEASE]
    return order[order.index(stage) + 1]


def test_skipped_stage_is_rejected():
    flow = StageFlow()
    with pytest.raises(StageFlowError, match="out of order|current stage"):
        flow.transition(_approval(Stage.DOCTYPES))
    with pytest.raises(StageFlowError, match="out of order|current stage"):
        flow.transition(_approval(Stage.RELEASE))


def test_out_of_order_approval_after_first_stage_is_rejected():
    flow = StageFlow().transition(_approval(Stage.PLAN))
    with pytest.raises(StageFlowError, match="out of order|current stage"):
        flow.transition(_approval(Stage.PERMISSIONS))


def test_approval_for_wrong_stage_after_completion_is_rejected():
    flow = StageFlow()
    for stage in (Stage.PLAN, Stage.DOCTYPES, Stage.PERMISSIONS, Stage.UI_TESTS, Stage.RELEASE):
        flow = flow.transition(_approval(stage))
    with pytest.raises(StageFlowError, match="already complete"):
        flow.transition(_approval(Stage.RELEASE))


def test_reject_records_rejection_without_advancing():
    flow = StageFlow()
    rejected = flow.reject(Stage.PLAN, "needs clarification")
    assert rejected.current_stage is Stage.PLAN
    assert rejected.records[-1].status is StageStatus.REJECTED
    assert rejected.records[-1].reason == "needs clarification"
    # A subsequent approval for the same stage can still proceed.
    approved = rejected.transition(_approval(Stage.PLAN))
    assert approved.current_stage is Stage.DOCTYPES


def test_reject_wrong_stage_is_rejected():
    flow = StageFlow()
    with pytest.raises(StageFlowError, match="current stage"):
        flow.reject(Stage.DOCTYPES, "wrong stage")


def test_records_are_immutable():
    flow = StageFlow().transition(_approval(Stage.PLAN))
    records = flow.records
    with pytest.raises(TypeError):
        records[0] = records[0]  # type: ignore[index]
    assert dataclasses.is_dataclass(records[0]) and records[0].__dataclass_params__.frozen


def test_stage_record_stores_only_normalized_fields():
    approval = _approval(Stage.PLAN)
    record = build_stage_record(Stage.PLAN, StageStatus.COMPLETED, approval.reason, approval=approval)
    normalized = record.to_record()
    assert set(normalized) == {"schema_version", "stage", "status", "reason", "stage_digest"}
    assert normalized["stage"] == "PLAN"
    assert normalized["status"] == "COMPLETED"


def test_approval_digest_binds_approval_contents():
    approval = _approval(Stage.PLAN, "operator-1", "ready")
    assert approval.approval_digest == approval_digest(approval)
    other = _approval(Stage.PLAN, "operator-1", "ready")
    assert approval.approval_digest == other.approval_digest
    tampered_reason = build_stage_approval(Stage.PLAN, "operator-1", "different")
    assert approval.approval_digest != tampered_reason.approval_digest


def test_stage_record_digest_binds_approval():
    approval = _approval(Stage.PLAN, "operator-1", "ready")
    record = build_stage_record(Stage.PLAN, StageStatus.COMPLETED, approval.reason, approval=approval)
    assert verify_stage_record(record, approval)
    other_approval = _approval(Stage.PLAN, "operator-2", "ready")
    assert not verify_stage_record(record, other_approval)


def test_tampered_record_digest_is_detected():
    approval = _approval(Stage.PLAN, "operator", "ready")
    record = build_stage_record(Stage.PLAN, StageStatus.COMPLETED, "ready", approval=approval)
    tampered_status = StageRecord(
        stage=record.stage,
        status=StageStatus.REJECTED,
        reason=record.reason,
        stage_digest=record.stage_digest,
    )
    assert not verify_stage_record(tampered_status, approval)
    tampered_reason = StageRecord(
        stage=record.stage,
        status=record.status,
        reason="different reason",
        stage_digest=record.stage_digest,
    )
    assert not verify_stage_record(tampered_reason, approval)
    # A record with a syntactically valid but incorrect digest is accepted as a
    # stored value; verification against the approval detects the mismatch.
    wrong_digest = StageRecord(
        stage=record.stage,
        status=record.status,
        reason=record.reason,
        stage_digest="0" * 64,
    )
    assert StageFlow((wrong_digest,)).records[0].stage_digest == "0" * 64
    assert not verify_stage_record(wrong_digest, approval)


def test_tampered_approval_digest_is_rejected():
    approval = _approval(Stage.PLAN, "operator", "ready")
    with pytest.raises(StageFlowError, match="approval_digest"):
        StageApproval(
            stage=Stage.PLAN,
            approver_id="operator",
            reason="ready",
            approval_digest="0" * 64,
        )
    # A forged approval cannot be constructed because __post_init__ recomputes
    # and verifies the digest over the approval's contents.
    with pytest.raises(StageFlowError, match="approval_digest"):
        StageApproval(
            stage=approval.stage,
            approver_id=approval.approver_id,
            reason=approval.reason,
            approval_digest="0" * 64,
        )


def test_replay_from_records_reproduces_state():
    original = (
        StageFlow()
        .transition(_approval(Stage.PLAN))
        .transition(_approval(Stage.DOCTYPES))
        .transition(_approval(Stage.PERMISSIONS))
    )
    replayed = parse_stage_flow(original.to_record())
    assert replayed.current_stage == original.current_stage
    assert replayed.is_complete == original.is_complete
    assert replayed.flow_digest() == original.flow_digest()
    assert len(replayed.records) == len(original.records)
    for replayed_record, original_record in zip(replayed.records, original.records):
        assert replayed_record.stage == original_record.stage
        assert replayed_record.status == original_record.status
        assert replayed_record.stage_digest == original_record.stage_digest


def test_replay_rejects_out_of_order_records():
    raw = {
        "schema_version": 1,
        "records": [
            {
                "schema_version": 1,
                "stage": "DOCTYPES",
                "status": "COMPLETED",
                "reason": "approved",
                "stage_digest": "0" * 64,
            }
        ],
    }
    with pytest.raises(StageFlowError, match="out of order"):
        parse_stage_flow(raw)


def test_replay_rejects_duplicate_records():
    raw = {
        "schema_version": 1,
        "records": [
            {
                "schema_version": 1,
                "stage": "PLAN",
                "status": "COMPLETED",
                "reason": "approved",
                "stage_digest": "0" * 64,
            },
            {
                "schema_version": 1,
                "stage": "PLAN",
                "status": "COMPLETED",
                "reason": "approved again",
                "stage_digest": "0" * 64,
            },
        ],
    }
    with pytest.raises(StageFlowError, match="duplicate"):
        parse_stage_flow(raw)


def test_parse_stage_flow_rejects_unknown_fields_and_bad_schema():
    with pytest.raises(StageFlowError, match="unknown fields"):
        parse_stage_flow({"schema_version": 1, "records": [], "extra": "x"})
    with pytest.raises(StageFlowError, match="schema_version"):
        parse_stage_flow({"schema_version": 2, "records": []})
    raw = {
        "schema_version": 1,
        "records": [
            {
                "schema_version": 1,
                "stage": "PLAN",
                "status": "COMPLETED",
                "reason": "approved",
                "stage_digest": "0" * 64,
                "extra": "x",
            }
        ],
    }
    with pytest.raises(StageFlowError, match="unknown or missing fields"):
        parse_stage_flow(raw)


def test_build_stage_approval_validates_inputs():
    with pytest.raises(StageFlowError, match="approver_id"):
        build_stage_approval(Stage.PLAN, "bad id!", "ok")
    with pytest.raises(StageFlowError, match="reason"):
        build_stage_approval(Stage.PLAN, "operator", "")
    with pytest.raises(StageFlowError, match="reason"):
        build_stage_approval(Stage.PLAN, "operator", "a\nmultiline")


def test_stage_record_validates_inputs():
    with pytest.raises(StageFlowError, match="stage"):
        StageRecord(stage="PLAN", status=StageStatus.COMPLETED, reason="ok", stage_digest="0" * 64)  # type: ignore[arg-type]
    with pytest.raises(StageFlowError, match="status"):
        StageRecord(stage=Stage.PLAN, status="COMPLETED", reason="ok", stage_digest="0" * 64)  # type: ignore[arg-type]
    with pytest.raises(StageFlowError, match="stage_digest"):
        StageRecord(stage=Stage.PLAN, status=StageStatus.COMPLETED, reason="ok", stage_digest="not-a-digest")


def test_flow_digest_changes_with_each_record():
    empty = StageFlow()
    first = empty.transition(_approval(Stage.PLAN))
    second = first.transition(_approval(Stage.DOCTYPES))
    assert empty.flow_digest() != first.flow_digest()
    assert first.flow_digest() != second.flow_digest()


def test_stage_record_without_approval_has_digest_over_stored_fields():
    record = build_stage_record(Stage.PLAN, StageStatus.REJECTED, "needs clarification")
    assert record.stage_digest == stage_record_digest(record)
    assert verify_stage_record(record, _approval(Stage.PLAN)) is False


def test_flow_records_must_be_tuple():
    record = build_stage_record(Stage.PLAN, StageStatus.COMPLETED, "ok")
    with pytest.raises(StageFlowError, match="immutable tuple"):
        StageFlow([record])  # type: ignore[arg-type]
