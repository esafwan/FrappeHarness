from pathlib import Path

from frappe_harness.metadata_verifier import verify_metadata_plan
from frappe_harness.provider_plan import build_metadata_plan
from frappe_harness.spec_io import load_project_spec


def _plan():
    return build_metadata_plan(load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json"))


def _observed():
    return {
        "Project": {"data": {"fields": [
            {"fieldname": "title", "fieldtype": "Data", "reqd": 1, "unique": 0},
            {"fieldname": "description", "fieldtype": "Small Text", "reqd": 0, "unique": 0},
            {"fieldname": "active", "fieldtype": "Check", "reqd": 0, "unique": 0, "default": True},
        ], "permissions": [
            {"role": "Task Manager", "permlevel": 0, "read": 1, "create": 1, "write": 1, "delete": 1},
            {"role": "Task User", "permlevel": 0, "read": 1, "create": 0, "write": 0, "delete": 0},
        ]}},
        "Task": {"fields": [
            {"fieldname": "title", "fieldtype": "Data", "reqd": 1, "unique": 0},
            {"fieldname": "description", "fieldtype": "Text Editor", "reqd": 0, "unique": 0},
            {"fieldname": "project", "fieldtype": "Link", "reqd": 1, "unique": 0, "options": "Project"},
            {"fieldname": "status", "fieldtype": "Select", "reqd": 1, "unique": 0, "default": "Open", "options": "Open\nIn Progress\nCompleted"},
            {"fieldname": "priority", "fieldtype": "Select", "reqd": 1, "unique": 0, "default": "Medium", "options": "Low\nMedium\nHigh"},
            {"fieldname": "due_date", "fieldtype": "Date", "reqd": 0, "unique": 0},
        ], "permissions": [
            {"role": "Task Manager", "permlevel": 0, "read": 1, "create": 1, "write": 1, "delete": 1},
            {"role": "Task User", "permlevel": 0, "read": 1, "create": 1, "write": 1, "delete": 0},
        ]},
    }


def test_matches_normalized_frappectl_doctype_json():
    report = verify_metadata_plan(_plan(), _observed())
    assert report.matches


def test_reports_field_link_select_and_permission_drift():
    observed = _observed()
    task = observed["Task"]
    fields = {field["fieldname"]: field for field in task["fields"]}
    fields["project"]["options"] = "Missing"
    fields["status"]["options"] = "Open\nDone"
    task["permissions"][1]["write"] = 0
    report = verify_metadata_plan(_plan(), observed)
    assert not report.matches
    assert {mismatch.code for mismatch in report.mismatches} >= {"link_target", "select_options", "permission"}


def test_reports_missing_field_and_ignores_framework_extras():
    observed = _observed()
    observed["Project"]["data"]["fields"] = [{"fieldname": "name", "fieldtype": "Data"}]
    observed["Task"]["owner"] = "Administrator"
    report = verify_metadata_plan(_plan(), observed)
    assert any(mismatch.code == "missing_field" and mismatch.path.endswith("fields.title") for mismatch in report.mismatches)
