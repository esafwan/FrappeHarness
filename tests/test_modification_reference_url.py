"""Checked-in non-destructive modification evidence for spec §19.7."""

from __future__ import annotations

import json
from pathlib import Path

from frappe_harness.artifact_audit import audit_compilation
from frappe_harness.compiler import compile_project
from frappe_harness.contracts import FieldType, validate_project_spec
from frappe_harness.schema_diff import ChangeDisposition, classify_schema_diff
from frappe_harness.spec_io import load_project_spec


_FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_task_tracker_v2_reference_url_is_the_exact_safe_frontend_modification() -> None:
    installed = load_project_spec(_FIXTURES / "task_tracker.json")
    confirmed = load_project_spec(_FIXTURES / "task_tracker_v2_reference_url.json")

    assert confirmed.version == 2
    assert validate_project_spec(confirmed).valid

    task = next(entity for entity in confirmed.entities if entity.name == "task")
    reference_url = next(field for field in task.fields if field.name == "reference_url")
    assert reference_url.field_type is FieldType.URL
    assert not reference_url.required and not reference_url.unique and reference_url.default is None
    assert task.fields[-1] == reference_url

    installed_task_form = next(screen for screen in installed.screens if screen.name == "task_form")
    task_list = next(screen for screen in confirmed.screens if screen.name == "task_list")
    task_form = next(screen for screen in confirmed.screens if screen.name == "task_form")
    assert "reference_url" not in task_list.fields
    assert task_form.fields == (*installed_task_form.fields, "reference_url")

    report = classify_schema_diff(installed, confirmed)
    assert report.safe
    assert [(change.code, change.path, change.disposition) for change in report.changes] == [
        ("optional_field_added", "entities.task.fields.reference_url", ChangeDisposition.SAFE_ADDITIVE),
        ("screen_changed", "screens.task_form", ChangeDisposition.SAFE_UPDATE),
    ]

    result = compile_project(confirmed)
    assert audit_compilation(result).valid
    task_doctype = json.loads(result.text("task_tracker/task_tracker/task_tracker/doctype/task/task.json"))
    rendered = next(field for field in task_doctype["fields"] if field["fieldname"] == "reference_url")
    # Frappe 16 represents the semantic URL vocabulary as a Data field with
    # URL options; the fixed frontend retains the stronger URL input control.
    assert rendered["fieldtype"] == "Data" and rendered["options"] == "URL"
    assert rendered["reqd"] == 0 and rendered["unique"] == 0

    manifest = json.loads(result.text("task_tracker/harness/frontend-manifest.json"))
    for role in manifest["roles"]:
        entity = next(entity for entity in role["entities"] if entity["name"] == "task")
        field = next(field for field in entity["fields"] if field["name"] == "reference_url")
        assert field["field_type"] == "URL"
        assert field["component"] == "url-input"
        assert field["creatable"] is True and field["editable"] is True
        form = next(route for route in role["routes"] if route["id"] == "task_form")
        listing = next(route for route in role["routes"] if route["id"] == "task_list")
        assert form["fields"][-1] == "reference_url"
        assert "reference_url" not in listing["fields"]
