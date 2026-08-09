from __future__ import annotations

import pytest

from frappe_harness.frappe_reconciliation_probe import (
    FrappeMigrationInspectionConfig,
    FrappeMigrationInspectionError,
    FrappeMigrationInspectionProbe,
)
from frappe_harness.frappe_rest_provider import FrappeRestProvider
from frappe_harness.frappe_reconciliation_probe import FrappeMetadataFieldExpectation
from frappe_harness.provider_plan import build_metadata_plan
from frappe_harness.run_store import LockTarget
from frappe_harness.spec_io import load_project_spec

from test_frappe_rest_provider import ScriptedTransport, _WireResponse


def _probe(payload):
    target = LockTarget("/bench", "dev.local", "task_tracker")
    return FrappeMigrationInspectionProbe(
        FrappeRestProvider("https://example.invalid", None, transport=ScriptedTransport(_WireResponse(200, payload))),
        FrappeMigrationInspectionConfig(target, "a" * 64, "b" * 64),
    ), target


def test_frappe_metadata_probe_returns_exact_read_only_facts():
    probe, target = _probe(b'{"data":{"fields":[{"fieldname":"reference_url","fieldtype":"Data","options":"URL"}]}}')
    facts = probe.inspect(target)
    assert facts.target == target
    assert facts.spec_hash == "a" * 64
    assert facts.plan_hash == "b" * 64
    assert facts.outcome == "inspected"
    assert "frappe-rest:inspect_meta:200:" in facts.reference


def test_frappe_metadata_probe_fails_closed_on_missing_field_or_target_mismatch():
    probe, target = _probe(b'{"data":{"fields":[]}}')
    with pytest.raises(FrappeMigrationInspectionError, match="reference_url"):
        probe.inspect(target)
    with pytest.raises(FrappeMigrationInspectionError, match="target"):
        probe.inspect(LockTarget("/other", target.site, target.app))


def test_frappe_metadata_probe_can_be_driven_by_a_v1_provider_plan():
    target = LockTarget("/bench", "dev.local", "task_tracker")
    spec = load_project_spec("fixtures/task_tracker.json")
    plan = build_metadata_plan(spec)
    task = next(operation for operation in plan.operations if operation.doctype == "Task")
    doctype_by_entity = {operation.entity: operation.doctype for operation in plan.operations}
    payload = {"data": {"fields": [{
        "fieldname": field.name,
        "fieldtype": field.frappe_field_type,
        "options": field.frappe_options or ("\n".join(field.select_options) if field.frappe_field_type == "Select" else doctype_by_entity.get(field.link_target or "")),
    } for field in task.fields]}}
    provider = FrappeRestProvider("https://example.invalid", None, transport=ScriptedTransport(_WireResponse(200, __import__("json").dumps(payload).encode())))
    probe = FrappeMigrationInspectionProbe(provider, FrappeMigrationInspectionConfig.from_plan(target, plan, doctype="Task"))
    assert probe.inspect(target).spec_hash == plan.spec_hash
