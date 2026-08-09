from __future__ import annotations

import pytest

from frappe_harness.lifecycle_contract import LifecycleState
from frappe_harness.metadata_execution import (
    MetadataVerificationAdmissionError,
    RunBoundMetadataVerifier,
)
from frappe_harness.migration_execution import RunBoundMigrationExecutor
from frappe_harness.schema_diff import classify_schema_diff

from test_migration_execution import FakeProvider, _prepared_store


def _observed(plan):
    documents = {}
    for operation in plan.operations:
        fields = []
        for field in operation.fields:
            options = field.frappe_options
            if field.link_target:
                target = next(item.doctype for item in plan.operations if item.entity == field.link_target)
                options = target
            elif field.select_options:
                options = "\n".join(field.select_options)
            fields.append({
                "fieldname": field.name,
                "fieldtype": field.frappe_field_type or field.field_type,
                "reqd": 1 if field.required else 0,
                "unique": 1 if field.unique else 0,
                "default": field.default,
                "options": options,
            })
        documents[operation.doctype] = {
            "fields": fields,
            "permissions": [
                {"role": permission.role, "read": permission.read, "create": permission.create,
                 "write": permission.write, "delete": permission.delete, "permlevel": 0}
                for permission in operation.permissions
            ],
        }
    return documents


def test_matching_metadata_passes_only_after_migration(tmp_path):
    spec, plan, compilation, store, run, approval, backup, checkpoint = _prepared_store(tmp_path)
    RunBoundMigrationExecutor(store).execute(
        run.id, plan=plan, compilation=compilation, schema_diff=classify_schema_diff(spec, spec),
        approval_id=approval.id, backup_evidence_id=backup.id, checkpoint_evidence_id=checkpoint.id,
        provider=FakeProvider(),
    )
    result = RunBoundMetadataVerifier(store).verify(run.id, plan=plan, observed_doctypes=_observed(plan))
    assert result.report.matches
    assert result.snapshot is not None
    assert result.snapshot.snapshot.record_for(LifecycleState.METADATA_VERIFIED).status.value == "passed"


def test_mismatched_metadata_is_recorded_but_does_not_pass(tmp_path):
    spec, plan, compilation, store, run, approval, backup, checkpoint = _prepared_store(tmp_path)
    RunBoundMigrationExecutor(store).execute(
        run.id, plan=plan, compilation=compilation, schema_diff=classify_schema_diff(spec, spec),
        approval_id=approval.id, backup_evidence_id=backup.id, checkpoint_evidence_id=checkpoint.id,
        provider=FakeProvider(),
    )
    observed = _observed(plan)
    observed[next(iter(observed))]["fields"][0]["fieldtype"] = "Unknown"
    result = RunBoundMetadataVerifier(store).verify(run.id, plan=plan, observed_doctypes=observed)
    assert not result.report.matches
    assert result.snapshot is None
    assert store.get_lifecycle_snapshot(run.id).snapshot.record_for(LifecycleState.METADATA_VERIFIED).status.value == "pending"


def test_metadata_requires_migration_and_exact_plan(tmp_path):
    spec, plan, compilation, store, run, approval, backup, checkpoint = _prepared_store(tmp_path)
    with pytest.raises(MetadataVerificationAdmissionError, match="MIGRATION_APPLIED"):
        RunBoundMetadataVerifier(store).verify(run.id, plan=plan, observed_doctypes={})


def test_metadata_rejects_unbound_migration_receipt(monkeypatch, tmp_path):
    spec, plan, compilation, store, run, approval, backup, checkpoint = _prepared_store(tmp_path)
    RunBoundMigrationExecutor(store).execute(
        run.id, plan=plan, compilation=compilation, schema_diff=classify_schema_diff(spec, spec),
        approval_id=approval.id, backup_evidence_id=backup.id, checkpoint_evidence_id=checkpoint.id,
        provider=FakeProvider(),
    )
    original = store.get_lifecycle_evidence_payload

    def tampered(evidence_id):
        payload = dict(original(evidence_id))
        if payload.get("provider") == "frappe-native":
            payload["plan_hash"] = "0" * 64
        return payload

    monkeypatch.setattr(store, "get_lifecycle_evidence_payload", tampered)
    with pytest.raises(MetadataVerificationAdmissionError, match="does not bind"):
        RunBoundMetadataVerifier(store).verify(run.id, plan=plan, observed_doctypes=_observed(plan))
