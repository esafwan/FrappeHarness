"""VAL-01 positive/negative scope and supported-vocabulary matrix."""

from dataclasses import replace
from pathlib import Path

import pytest

from frappe_harness.contracts import EntitySpec, FieldSpec, FieldType, ProjectSpec, validate_project_spec
from frappe_harness.spec_io import load_project_spec


def _report(spec: ProjectSpec):
    return validate_project_spec(spec)


def task_tracker() -> ProjectSpec:
    return load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")


@pytest.mark.parametrize("entity_count", [1, 2, 5])
def test_accepts_supported_entity_count_boundaries(entity_count):
    base = task_tracker()
    entities = tuple(base.entities[:entity_count])
    names = {entity.name for entity in entities}
    screens = tuple(screen for screen in base.screens if screen.entity in names)
    assert _report(replace(base, entities=entities, screens=screens)).valid


@pytest.mark.parametrize("entity_count", [0, 6])
def test_rejects_entity_count_outside_v1_scope(entity_count):
    base = task_tracker()
    entities = tuple(base.entities[:entity_count]) if entity_count else ()
    if entity_count == 6:
        entities = tuple(base.entities) + tuple(
            EntitySpec(f"extra_{index}", f"Extra {index}", base.entities[0].fields, base.entities[0].permissions)
            for index in range(4)
        )
    report = _report(replace(base, entities=entities))
    assert "entity_count" in {issue.code for issue in report.errors}


@pytest.mark.parametrize("field_type", list(FieldType))
def test_all_declared_v1_field_types_are_accepted_in_a_valid_shape(field_type):
    base = task_tracker()
    if field_type is FieldType.SELECT:
        field = FieldSpec("extra", "Extra", field_type, options=("One",), default="One")
    elif field_type is FieldType.LINK:
        field = FieldSpec("extra", "Extra", field_type, options=("project",))
    elif field_type in (FieldType.SECTION_BREAK, FieldType.COLUMN_BREAK):
        field = FieldSpec("extra", "Extra", field_type)
    else:
        field = FieldSpec("extra", "Extra", field_type)
    entity = replace(base.entities[0], fields=base.entities[0].fields + (field,))
    report = _report(replace(base, entities=(entity, base.entities[1])))
    assert "unsupported_field_type" not in {issue.code for issue in report.errors}


@pytest.mark.parametrize(
    "mutator,code",
    [
        (lambda spec: replace(spec, name="Bad Name"), "invalid_identifier"),
        (lambda spec: replace(spec, entities=spec.entities + (spec.entities[0],)), "duplicate_entity"),
        (lambda spec: replace(spec, roles=()), "missing_roles"),
    ],
)
def test_rejects_scope_identifier_and_role_shape_errors(mutator, code):
    report = _report(mutator(task_tracker()))
    assert code in {issue.code for issue in report.errors}
