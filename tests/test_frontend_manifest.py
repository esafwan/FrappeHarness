from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from frappe_harness.contracts import FieldSpec, FieldType
from frappe_harness.frontend_manifest import (
    COMPONENT_BY_FIELD_TYPE,
    FrontendManifestError,
    build_frontend_manifest,
    frontend_manifest_json,
)
from frappe_harness.spec_io import load_project_spec


def _spec():
    return load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")


def test_manifest_is_repeatable_hash_bound_and_contains_data_only_routes() -> None:
    first = build_frontend_manifest(_spec())
    second = build_frontend_manifest(_spec())

    assert first == second
    payload = json.loads(frontend_manifest_json(first))
    assert payload["spec_hash"] == first.spec_hash
    assert payload["manifest_hash"] == first.manifest_hash
    rendered = frontend_manifest_json(first)
    assert "<script" not in rendered
    assert "subprocess" not in rendered
    assert ".vue" not in rendered


def test_manifest_hash_matches_compact_browser_canonical_bytes() -> None:
    manifest = build_frontend_manifest(_spec())
    payload = json.loads(frontend_manifest_json(manifest))
    unhashed = {key: value for key, value in payload.items() if key != "manifest_hash"}
    compact = json.dumps(unhashed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    import hashlib
    assert manifest.manifest_hash == hashlib.sha256(compact.encode("utf-8")).hexdigest()


def test_manifest_scopes_entities_routes_and_actions_by_role() -> None:
    manifest = build_frontend_manifest(_spec())
    user = next(item for item in manifest.roles if item.role == "Task User")
    manager = next(item for item in manifest.roles if item.role == "Task Manager")

    assert [entity.name for entity in user.entities] == ["project", "task"]
    user_task = next(entity for entity in user.entities if entity.name == "task")
    manager_task = next(entity for entity in manager.entities if entity.name == "task")
    user_project = next(entity for entity in user.entities if entity.name == "project")
    assert user_task.actions.read and user_task.actions.create and user_task.actions.write
    assert not user_task.actions.delete
    assert not user_project.actions.create and not user_project.actions.write and not user_project.actions.delete
    assert manager_task.actions.delete
    assert [item.route_id for item in user.navigation] == ["project_list", "task_list"]
    assert {route.path for route in user.routes} == {
        "/task_tracker/project", "/task_tracker/project/:name",
        "/task_tracker/task", "/task_tracker/task/:name",
    }


def test_list_routes_allow_only_declared_fields_operators_and_directions() -> None:
    manifest = build_frontend_manifest(_spec())
    manager = next(item for item in manifest.roles if item.role == "Task Manager")
    task_list = next(route for route in manager.routes if route.id == "task_list")

    assert [field.name for field in task_list.allowed_filters] == ["project", "status", "priority"]
    assert next(field for field in task_list.allowed_filters if field.name == "project").operators == ("equals", "in")
    assert next(field for field in task_list.allowed_filters if field.name == "status").operators == ("equals", "in")
    assert task_list.allowed_orders == (
        "title:asc", "title:desc", "project:asc", "project:desc", "status:asc", "status:desc",
        "priority:asc", "priority:desc", "due_date:asc", "due_date:desc",
    )
    assert task_list.record_id_field == "name"


def test_manifest_projects_fixed_components_visibility_help_and_frappe_link_target() -> None:
    spec = _spec()
    extra = (
        FieldSpec("internal_note", "Internal note", FieldType.SMALL_TEXT, description="Private", hidden=True),
        FieldSpec("locked_note", "Locked note", FieldType.TEXT, description="Read only", read_only=True),
        FieldSpec("details_section", "Details", FieldType.SECTION_BREAK),
        FieldSpec("details_column", "Details column", FieldType.COLUMN_BREAK),
        FieldSpec("email", "Email", FieldType.EMAIL),
        FieldSpec("phone", "Phone", FieldType.PHONE),
        FieldSpec("body", "Body", FieldType.TEXT_EDITOR),
    )
    form = next(screen for screen in spec.screens if screen.name == "task_form")
    enriched = replace(
        spec,
        entities=tuple(replace(entity, fields=entity.fields + extra) if entity.name == "task" else entity for entity in spec.entities),
        screens=tuple(replace(screen, fields=form.fields + tuple(field.name for field in extra if not field.hidden)) if screen.name == "task_form" else screen for screen in spec.screens),
    )

    task_manifest = next(entity for entity in next(role for role in build_frontend_manifest(enriched).roles if role.role == "Task Manager").entities if entity.name == "task")
    fields = {field.name: field for field in task_manifest.fields}
    assert "internal_note" not in fields
    assert fields["locked_note"].read_only is True
    assert fields["locked_note"].editable is False
    assert fields["locked_note"].creatable is False
    assert fields["locked_note"].description == "Read only"
    assert fields["project"].component == "link-autocomplete"
    assert fields["project"].link_target_entity == "project"
    assert fields["project"].link_target == "Project"
    assert fields["details_section"].component == "form-section"
    assert fields["details_column"].component == "column-break"
    assert fields["email"].component == "email-input"
    assert fields["phone"].component == "telephone-input"
    assert fields["body"].component == "rich-text-editor"


def test_component_registry_covers_exactly_every_supported_field_type() -> None:
    assert set(COMPONENT_BY_FIELD_TYPE) == set(FieldType)
    assert all(isinstance(component, str) and component for component in COMPONENT_BY_FIELD_TYPE.values())

    spec = _spec()
    all_types = tuple(
        FieldSpec(
            f"control_{field_type.name.lower()}",
            field_type.value,
            field_type,
            options=("One", "Two") if field_type is FieldType.SELECT else (("project",) if field_type is FieldType.LINK else ()),
        )
        for field_type in FieldType
    )
    form = next(screen for screen in spec.screens if screen.name == "task_form")
    task = next(entity for entity in spec.entities if entity.name == "task")
    enriched = replace(
        spec,
        entities=tuple(
            replace(
                entity,
                fields=all_types,
                title_field=None,
                default_sort_field=None,
                list_columns=(),
                standard_filters=(),
                search_fields=(),
            ) if entity.name == "task" else entity
            for entity in spec.entities
        ),
        screens=tuple(
            replace(screen, fields=tuple(field.name for field in all_types))
            if screen.name == form.name
            else screen
            for screen in spec.screens
            if screen.entity != task.name or screen.name == form.name
        ),
    )
    role = next(role for role in build_frontend_manifest(enriched).roles if role.role == "Task Manager")
    fields = {field.name: field for entity in role.entities if entity.name == "task" for field in entity.fields}
    assert {
        field.field_type: field.component
        for name, field in fields.items()
        if name.startswith("control_")
    } == {field_type.value: component for field_type, component in COMPONENT_BY_FIELD_TYPE.items()}


def test_manifest_rejects_layout_or_hidden_fields_in_list_projection() -> None:
    spec = _spec()
    list_screen = next(screen for screen in spec.screens if screen.name == "task_list")
    layout = FieldSpec("section", "Section", FieldType.SECTION_BREAK)
    with pytest.raises(FrontendManifestError, match="layout field"):
        build_frontend_manifest(
            replace(
                spec,
                entities=tuple(replace(entity, fields=entity.fields + (layout,)) if entity.name == "task" else entity for entity in spec.entities),
                screens=tuple(replace(screen, fields=list_screen.fields + ("section",)) if screen.name == "task_list" else screen for screen in spec.screens),
            )
        )


def test_manifest_hides_entities_without_read_permission_and_never_adds_fallback_routes() -> None:
    spec = _spec()
    project = next(entity for entity in spec.entities if entity.name == "project")
    no_project_read = replace(
        project,
        permissions=tuple(
            replace(permission, read=False) if permission.role == "Task User" else permission
            for permission in project.permissions
        ),
    )
    hidden = replace(spec, entities=(no_project_read,) + tuple(entity for entity in spec.entities if entity.name != "project"))
    user = next(item for item in build_frontend_manifest(hidden).roles if item.role == "Task User")

    assert [entity.name for entity in user.entities] == ["task"]
    assert all(route.entity == "task" for route in user.routes)
    assert [item.entity for item in user.navigation] == ["task"]


def test_invalid_specs_do_not_produce_manifests() -> None:
    spec = _spec()
    task = next(entity for entity in spec.entities if entity.name == "task")
    invalid = replace(task, fields=task.fields + (FieldSpec("bad", "Bad", FieldType.SELECT),))
    with pytest.raises(ValueError, match="Invalid project specification"):
        build_frontend_manifest(replace(spec, entities=tuple(invalid if entity.name == "task" else entity for entity in spec.entities)))
