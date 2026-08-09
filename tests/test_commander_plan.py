from dataclasses import replace
from pathlib import Path

import pytest

from frappe_harness.commander_plan import (
    CommanderAddField,
    UnsupportedCommanderOperation,
    build_commander_compatibility_plan,
    commander_plan_json,
)
from frappe_harness.provider_plan import build_metadata_plan
from frappe_harness.spec_io import load_project_spec


def _plan_without_permissions():
    spec = load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")
    source = build_metadata_plan(spec, provider="commander-spike")
    return replace(
        source,
        operations=tuple(replace(operation, permissions=()) for operation in source.operations),
    )


def test_typed_commander_plan_preserves_field_semantics_without_cli_text():
    plan = build_commander_compatibility_plan(_plan_without_permissions())

    assert [action.kind for action in plan.actions[:3]] == ["create_doctype", "add_field", "add_field"]
    task_project_field = next(
        action.arguments
        for action in plan.actions
        if isinstance(action.arguments, CommanderAddField) and action.arguments.name == "project"
    )
    assert task_project_field.link_target == "project"
    # §19 includes the optional rich-text Description between Subject and
    # Project, so the typed placement intent must preserve that field order.
    assert task_project_field.after == "description"
    rich_text_field = next(
        action.arguments
        for action in plan.actions
        if isinstance(action.arguments, CommanderAddField)
        and action.arguments.doctype == "Task"
        and action.arguments.name == "description"
    )
    assert rich_text_field.field_type == "Text Editor"
    rendered = commander_plan_json(plan)
    assert "new-doctype" not in rendered
    assert "--" not in rendered
    assert "shell" not in rendered


def test_commander_plan_serialization_is_deterministic_and_hash_bound():
    plan = build_commander_compatibility_plan(_plan_without_permissions())
    first = commander_plan_json(plan)
    second = commander_plan_json(build_commander_compatibility_plan(_plan_without_permissions()))

    assert first == second
    assert plan.source_spec_hash in first
    assert plan.source_plan_hash in first
    assert plan.plan_hash in first


def test_permission_rows_are_bound_for_typed_permission_update():
    spec = load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")
    plan = build_commander_compatibility_plan(build_metadata_plan(spec, provider="commander-spike"))
    create = plan.actions[0].arguments
    assert create.permissions
    assert {row[0] for row in create.permissions} == {"Task Manager", "Task User"}


def test_unknown_field_type_fails_closed():
    source = _plan_without_permissions()
    bad_field = replace(source.operations[0].fields[0], field_type="Table MultiSelect")
    bad_operation = replace(source.operations[0], fields=(bad_field,) + source.operations[0].fields[1:])
    bad_plan = replace(source, operations=(bad_operation,) + source.operations[1:])

    with pytest.raises(UnsupportedCommanderOperation, match="unsupported field type"):
        build_commander_compatibility_plan(bad_plan)


def test_unknown_source_operation_fails_closed():
    source = _plan_without_permissions()
    unsupported = replace(source.operations[0], kind="delete_doctype")
    bad_plan = replace(source, operations=(unsupported,) + source.operations[1:])

    with pytest.raises(UnsupportedCommanderOperation, match="unsupported kind"):
        build_commander_compatibility_plan(bad_plan)
