from pathlib import Path

import pytest

from frappe_harness.contracts import spec_hash, validate_project_spec
from frappe_harness.spec_io import SpecInputError, dump_project_spec, load_project_spec, project_from_mapping


def test_golden_fixture_round_trips_to_a_valid_spec():
    fixture = Path(__file__).parents[1] / "fixtures" / "task_tracker.json"
    spec = load_project_spec(fixture)

    assert validate_project_spec(spec).valid
    assert spec_hash(spec)
    assert '"task_tracker"' in dump_project_spec(spec)


def test_golden_fixture_matches_section_19_task_tracker_contract():
    fixture = Path(__file__).parents[1] / "fixtures" / "task_tracker.json"
    spec = load_project_spec(fixture)
    project, task = spec.entities

    assert project.title_field == "title"
    assert project.search_fields == project.list_columns == ("title",)
    assert project.standard_filters == ("active",)
    assert project.fields[-1].default is True
    assert project.fields[-1].in_standard_filter is True

    assert task.title_field == "title"
    assert task.search_fields == ("title",)
    assert task.list_columns == ("title", "project", "status", "priority", "due_date")
    assert task.standard_filters == ("project", "status", "priority")
    assert [(field.name, field.field_type.value) for field in task.fields] == [
        ("title", "Data"),
        ("description", "Text Editor"),
        ("project", "Link"),
        ("status", "Select"),
        ("priority", "Select"),
        ("due_date", "Date"),
    ]
    assert task.fields[2].options == ("project",)
    assert task.fields[3].options == ("Open", "In Progress", "Completed")
    assert task.fields[4].default == "Medium"
    assert all(field.in_list_view for field in task.fields if field.name in task.list_columns)
    assert all(field.in_standard_filter for field in task.fields if field.name in task.standard_filters)


@pytest.mark.parametrize("value", ["false", 0, 1, None])
def test_untrusted_flags_must_be_json_booleans(value):
    with pytest.raises(SpecInputError, match="required must be a JSON boolean"):
        project_from_mapping({
            "name": "app", "label": "App", "module": "app", "roles": [{"name": "User"}],
            "entities": [{
                "name": "thing", "label": "Thing",
                "fields": [{"name": "title", "label": "Title", "field_type": "Data", "required": value}],
                "permissions": [{"role": "User"}],
            }],
            "screens": [],
        })


def test_parser_preserves_supported_field_and_entity_projection_properties():
    spec = project_from_mapping({
        "name": "app", "label": "App", "module": "app", "roles": [{"name": "User"}],
        "entities": [{
            "name": "thing", "label": "Thing",
            "fields": [{
                "name": "title", "label": "Title", "field_type": "Data",
                "read_only": True, "hidden": False, "in_list_view": True,
                "in_standard_filter": True, "position": 3,
            }],
            "permissions": [{"role": "User"}],
            "title_field": "title", "default_sort_field": "title",
            "default_sort_order": "desc", "track_changes": False,
            "search_fields": ["title"], "list_columns": ["title"],
            "standard_filters": ["title"],
        }],
        "screens": [],
    })

    field = spec.entities[0].fields[0]
    assert (field.read_only, field.hidden, field.in_list_view, field.in_standard_filter, field.position) == (True, False, True, True, 3)
    entity = spec.entities[0]
    assert entity.title_field == entity.default_sort_field == "title"
    assert entity.default_sort_order == "desc"
    assert entity.track_changes is False
    assert entity.search_fields == entity.list_columns == entity.standard_filters == ("title",)


@pytest.mark.parametrize(
    ("property_name", "value", "message"),
    [
        ("read_only", "true", "read_only must be a JSON boolean"),
        ("hidden", 0, "hidden must be a JSON boolean"),
        ("in_list_view", None, "in_list_view must be a JSON boolean"),
        ("in_standard_filter", 1, "in_standard_filter must be a JSON boolean"),
        ("position", True, "position must be a non-negative JSON integer"),
        ("position", -1, "position must be a non-negative JSON integer"),
        ("position", "1", "position must be a non-negative JSON integer"),
    ],
)
def test_new_field_properties_fail_closed(property_name, value, message):
    field = {"name": "title", "label": "Title", "field_type": "Data", property_name: value}
    with pytest.raises(SpecInputError, match=message):
        project_from_mapping(_project_with_entity(fields=[field]))


@pytest.mark.parametrize(
    ("property_name", "value", "message"),
    [
        ("title_field", 1, "title_field must be a JSON string or null"),
        ("default_sort_field", False, "default_sort_field must be a JSON string or null"),
        ("default_sort_order", "DESC", "default_sort_order must be 'asc' or 'desc'"),
        ("default_sort_order", None, "default_sort_order must be 'asc' or 'desc'"),
        ("track_changes", "false", "track_changes must be a JSON boolean"),
        ("search_fields", "title", "search_fields must be an array of strings"),
        ("list_columns", ["title", 1], "list_columns must be an array of strings"),
        ("standard_filters", None, "standard_filters must be an array of strings"),
    ],
)
def test_new_entity_projection_properties_fail_closed(property_name, value, message):
    with pytest.raises(SpecInputError, match=message):
        project_from_mapping(_project_with_entity(**{property_name: value}))


def _project_with_entity(*, fields=None, **entity_properties):
    entity = {
        "name": "thing", "label": "Thing",
        "fields": fields or [{"name": "title", "label": "Title", "field_type": "Data"}],
        "permissions": [{"role": "User"}],
    }
    entity.update(entity_properties)
    return {
        "name": "app", "label": "App", "module": "app", "roles": [{"name": "User"}],
        "entities": [entity], "screens": [],
    }
