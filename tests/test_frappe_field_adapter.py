from dataclasses import replace
import json
from pathlib import Path

from frappe_harness.artifact_audit import audit_artifacts
from frappe_harness.compiler import canonical_json, compile_project
from frappe_harness.contracts import FieldSpec, FieldType
from frappe_harness.frappe_field_adapter import project_field_type
from frappe_harness.metadata_verifier import verify_metadata_plan
from frappe_harness.provider_plan import build_metadata_plan
from frappe_harness.spec_io import load_project_spec


def _url_spec():
    spec = load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")
    task = next(entity for entity in spec.entities if entity.name == "task")
    task = replace(task, fields=task.fields + (FieldSpec("reference_url", "Reference URL", FieldType.URL, position=6),))
    task = replace(task, list_columns=task.list_columns, standard_filters=task.standard_filters)
    return replace(spec, entities=tuple(task if entity.name == "task" else entity for entity in spec.entities), screens=tuple(replace(screen, fields=screen.fields + ("reference_url",)) if screen.name == "task_form" else screen for screen in spec.screens))


def test_semantic_url_projects_to_frappe_data_url_and_compiler_never_emits_url_fieldtype():
    assert project_field_type(FieldType.URL).fieldtype == "Data"
    assert project_field_type(FieldType.URL).options == "URL"
    result = compile_project(_url_spec())
    task = json.loads(result.text("task_tracker/task_tracker/task_tracker/doctype/task/task.json"))
    field = next(item for item in task["fields"] if item["fieldname"] == "reference_url")
    assert field == {"fieldname": "reference_url", "fieldtype": "Data", "hidden": 0, "in_list_view": 0, "in_standard_filter": 0, "label": "Reference URL", "options": "URL", "read_only": 0, "reqd": 0, "unique": 0}


def test_provider_metadata_expectation_requires_data_url_not_raw_url_fieldtype():
    spec = _url_spec()
    plan = build_metadata_plan(spec)
    task = next(operation for operation in plan.operations if operation.entity == "task")
    field = next(field for field in task.fields if field.name == "reference_url")
    assert (field.field_type, field.frappe_field_type, field.frappe_options) == ("URL", "Data", "URL")
    rendered = compile_project(spec)
    observed = {
        "Project": json.loads(rendered.text("task_tracker/task_tracker/task_tracker/doctype/project/project.json")),
        "Task": json.loads(rendered.text("task_tracker/task_tracker/task_tracker/doctype/task/task.json")),
    }
    assert verify_metadata_plan(plan, observed).matches
    next(item for item in observed["Task"]["fields"] if item["fieldname"] == "reference_url")["fieldtype"] = "URL"
    assert "field_type" in {item.code for item in verify_metadata_plan(plan, observed).mismatches}


def test_artifact_audit_rejects_raw_frappe_url_fieldtype():
    result = compile_project(_url_spec())
    artifacts = dict(result.artifacts)
    path = "task_tracker/task_tracker/task_tracker/doctype/task/task.json"
    document = json.loads(artifacts[path])
    next(item for item in document["fields"] if item["fieldname"] == "reference_url")["fieldtype"] = "URL"
    artifacts[path] = canonical_json(document)
    assert "unsupported_frappe_fieldtype" in {issue.code for issue in audit_artifacts(artifacts, result.ownership_manifest).issues}
