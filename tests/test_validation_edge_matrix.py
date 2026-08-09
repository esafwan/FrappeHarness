"""VAL-02/VAL-03 edge-case validation evidence."""

from dataclasses import replace
from pathlib import Path

import pytest

from frappe_harness.contracts import EntitySpec, FieldSpec, FieldType, PermissionSpec, RoleSpec
from frappe_harness.spec_io import load_project_spec


def _spec():
    return load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")


def _codes(spec):
    from frappe_harness.contracts import validate_project_spec
    report = validate_project_spec(spec)
    return {issue.code for issue in report.errors}, {issue.code for issue in report.issues}


@pytest.mark.parametrize(
    "field,code",
    [
        (FieldSpec("choice", "Choice", FieldType.SELECT), "select_options_required"),
        (FieldSpec("choice", "Choice", FieldType.SELECT, options=("Open",), default="Closed"), "invalid_select_default"),
        (FieldSpec("target", "Target", FieldType.LINK, options=()), "link_target_required"),
        (FieldSpec("target", "Target", FieldType.LINK, options=("missing",)), "unknown_link_target"),
    ],
)
def test_select_and_link_constraints_are_fail_closed(field, code):
    base = _spec()
    entity = replace(base.entities[0], fields=base.entities[0].fields + (field,))
    errors, _ = _codes(replace(base, entities=(entity, base.entities[1])))
    assert code in errors


def test_required_link_cycle_is_rejected_before_provider_planning():
    base = _spec()
    permissions = base.entities[0].permissions
    first = EntitySpec("first", "First", (FieldSpec("second", "Second", FieldType.LINK, required=True, options=("second",)),), permissions)
    second = EntitySpec("second", "Second", (FieldSpec("first", "First", FieldType.LINK, required=True, options=("first",)),), permissions)
    errors, _ = _codes(replace(base, entities=(first, second), screens=()))
    assert "required_link_cycle" in errors


def test_permission_matrix_requires_every_declared_role_per_entity():
    base = _spec()
    entity = replace(base.entities[0], permissions=(PermissionSpec("Task User", read=True),))
    errors, _ = _codes(replace(base, entities=(entity, base.entities[1])))
    assert "incomplete_permissions" in errors


def test_unknown_permission_role_is_rejected():
    base = _spec()
    entity = replace(base.entities[0], permissions=base.entities[0].permissions + (PermissionSpec("Ghost", read=True),))
    errors, _ = _codes(replace(base, entities=(entity, base.entities[1])))
    assert "unknown_role" in errors


def test_link_target_readability_is_reported_as_a_warning_for_frontend_review():
    base = _spec()
    roles = (RoleSpec("Editor"),)
    target = EntitySpec("target", "Target", (FieldSpec("title", "Title", FieldType.DATA),), (PermissionSpec("Editor", read=False),))
    source = EntitySpec("source", "Source", (FieldSpec("target", "Target", FieldType.LINK, options=("target",)),), (PermissionSpec("Editor", create=True, write=True),))
    errors, issues = _codes(replace(base, roles=roles, entities=(target, source), screens=()))
    assert not errors
    assert "link_target_not_readable" in issues
