from dataclasses import replace
from pathlib import Path

from frappe_harness.contracts import EntitySpec, FieldSpec, FieldType
from frappe_harness.schema_diff import ChangeDisposition, classify_schema_diff
from frappe_harness.spec_io import load_project_spec


def _spec():
    return load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")


def _replace_entity(spec, name, entity):
    return replace(spec, entities=tuple(entity if item.name == name else item for item in spec.entities))


def _replace_field(spec, entity_name, field_name, field):
    entity = next(item for item in spec.entities if item.name == entity_name)
    changed = replace(entity, fields=tuple(field if item.name == field_name else item for item in entity.fields))
    return _replace_entity(spec, entity_name, changed)


def test_unchanged_specs_have_an_empty_safe_report():
    report = classify_schema_diff(_spec(), _spec())
    assert report.safe
    assert not report.has_changes


def test_optional_field_and_new_doctype_are_safe_additive():
    installed = _spec()
    task = next(entity for entity in installed.entities if entity.name == "task")
    changed_task = replace(task, fields=task.fields + (FieldSpec("reference_url", "Reference URL", FieldType.URL),))
    note = EntitySpec("note", "Note", (FieldSpec("body", "Body", FieldType.LONG_TEXT, required=True),), task.permissions)
    confirmed = replace(installed, entities=tuple(changed_task if item.name == "task" else item for item in installed.entities) + (note,))

    report = classify_schema_diff(installed, confirmed)
    assert report.safe
    assert {(change.code, change.disposition) for change in report.changes} == {
        ("optional_field_added", ChangeDisposition.SAFE_ADDITIVE),
        ("entity_added", ChangeDisposition.SAFE_ADDITIVE),
    }


def test_required_field_with_valid_default_is_safe_but_without_one_is_blocked():
    installed = _spec()
    task = next(entity for entity in installed.entities if entity.name == "task")
    defaulted = replace(task, fields=task.fields + (FieldSpec("archived", "Archived", FieldType.CHECK, required=True, default=False),))
    safe_report = classify_schema_diff(installed, _replace_entity(installed, "task", defaulted))
    assert safe_report.safe
    assert safe_report.changes[0].code == "required_field_with_default_added"

    unsafe = replace(task, fields=task.fields + (FieldSpec("owner_note", "Owner Note", FieldType.DATA, required=True),))
    blocked_report = classify_schema_diff(installed, _replace_entity(installed, "task", unsafe))
    assert not blocked_report.safe
    assert blocked_report.blocked[0].code == "required_field_without_default"


def test_destructive_and_semantic_field_changes_are_blocked():
    installed = _spec()
    task = next(entity for entity in installed.entities if entity.name == "task")
    removed = replace(
        task,
        fields=tuple(field for field in task.fields if field.name != "due_date"),
        # Keep every projection valid so the classifier reaches the intended
        # destructive-change finding rather than failing the contract first.
        default_sort_field="priority",
        list_columns=tuple(field for field in task.list_columns if field != "due_date"),
    )
    confirmed = _replace_entity(installed, "task", removed)
    confirmed = replace(
        confirmed,
        screens=tuple(
            replace(screen, fields=tuple(field for field in screen.fields if field != "due_date"))
            if screen.entity == "task" else screen
            for screen in confirmed.screens
        ),
    )
    report = classify_schema_diff(installed, confirmed)
    assert not report.safe
    assert report.blocked[0].code == "field_removed"

    status = next(field for field in task.fields if field.name == "status")
    changed_status = replace(status, options=("Open", "Done"))
    report = classify_schema_diff(installed, _replace_field(installed, "task", "status", changed_status))
    assert not report.safe
    assert report.blocked[0].code == "field_options"


def test_permission_and_identity_changes_are_blocked_while_label_is_safe_update():
    installed = _spec()
    task = next(entity for entity in installed.entities if entity.name == "task")
    changed_permissions = replace(task, permissions=tuple(replace(permission, delete=True) if permission.role == "Task User" else permission for permission in task.permissions))
    report = classify_schema_diff(installed, _replace_entity(installed, "task", changed_permissions))
    assert not report.safe
    assert report.blocked[0].code == "permission_changed"

    title = next(field for field in task.fields if field.name == "title")
    report = classify_schema_diff(installed, _replace_field(installed, "task", "title", replace(title, label="Task subject")))
    assert report.safe
    assert report.changes[0].disposition is ChangeDisposition.SAFE_UPDATE

    report = classify_schema_diff(installed, replace(installed, module="renamed_module"))
    assert not report.safe
    assert report.blocked[0].code == "project_module"


def test_invalid_confirmed_contract_fails_closed():
    installed = _spec()
    task = next(entity for entity in installed.entities if entity.name == "task")
    invalid = replace(task, fields=task.fields + (FieldSpec("bad_select", "Bad select", FieldType.SELECT),))
    report = classify_schema_diff(installed, _replace_entity(installed, "task", invalid))
    assert not report.safe
    assert report.blocked[0].code == "invalid_spec"
    assert "select_options_required" in report.blocked[0].reason
