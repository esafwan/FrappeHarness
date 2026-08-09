"""Deterministic, data-only backend and REST verification templates.

These templates describe tests a trusted Frappe integration runner must execute
after installation.  They are intentionally not Python test code, shell text,
REST URLs, or executable callbacks.  The confirmed ``ProjectSpec`` is their
only input.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any, Mapping

from .compiler import canonical_json
from .contracts import EntitySpec, FieldSpec, FieldType, PermissionSpec, ProjectSpec, spec_hash, validate_project_spec


class TestTemplateError(ValueError):
    """Raised if a confirmed spec cannot produce constrained test templates."""


@dataclass(frozen=True)
class VerificationCase:
    """A declarative verification action understood by a future trusted runner."""

    id: str
    suite: str
    entity: str
    operation: str
    actor: str
    expected: str
    inputs: Mapping[str, Any]
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerificationTemplateResult:
    """Stable artifacts and semantic cases tied to one confirmed specification."""

    artifacts: Mapping[str, bytes]
    cases: tuple[VerificationCase, ...]
    spec_hash: str
    template_hash: str


_CRUD = ("read", "create", "write", "delete")
_LAYOUT_FIELD_TYPES = frozenset({FieldType.SECTION_BREAK, FieldType.COLUMN_BREAK})
_REST = (
    ("list", "GET"),
    ("read", "GET"),
    ("create", "POST"),
    ("update", "PATCH"),
    ("delete", "DELETE"),
)


def generate_verification_templates(spec: ProjectSpec) -> VerificationTemplateResult:
    """Produce a repeatable backend/REST case set for a valid confirmed spec."""

    report = validate_project_spec(spec)
    report.require_valid()
    entities = {entity.name: entity for entity in spec.entities}
    cases: list[VerificationCase] = []
    for entity in sorted(spec.entities, key=lambda item: item.name):
        cases.extend(_backend_cases(entity, entities))
        cases.extend(_permission_cases(entity))
        cases.extend(_rest_cases(entity))
    cases.extend(_anonymous_rest_cases(spec.entities))
    cases.sort(key=lambda item: item.id)

    data = {
        "version": 1,
        "target_frappe_major": 16,
        "spec_hash": spec_hash(spec),
        "cases": [asdict(case) for case in cases],
    }
    template_hash = sha256(canonical_json(data)).hexdigest()
    plan = data | {"template_hash": template_hash}
    artifacts: dict[str, bytes] = {
        "harness/verification-plan.json": canonical_json(plan),
        "harness/verification-summary.json": canonical_json(_summary(cases, spec_hash(spec), template_hash)),
    }
    for entity in sorted(spec.entities, key=lambda item: item.name):
        entity_cases = [asdict(case) for case in cases if case.entity == entity.name]
        artifacts[f"harness/tests/{entity.name}.json"] = canonical_json({
            "version": 1,
            "entity": entity.name,
            "doctype": entity.label,
            "spec_hash": spec_hash(spec),
            "cases": entity_cases,
        })
    return VerificationTemplateResult(dict(sorted(artifacts.items())), tuple(cases), spec_hash(spec), template_hash)


def _backend_cases(entity: EntitySpec, entities: Mapping[str, EntitySpec]) -> list[VerificationCase]:
    cases: list[VerificationCase] = []
    valid_payload = _valid_payload(entity)
    fixture_dependencies = tuple(sorted({field.options[0] for field in entity.fields if field.field_type is FieldType.LINK}))
    cases.append(_case(entity, "backend", "create_valid", "administrator", "success", {"payload": valid_payload, "fixture_entities": fixture_dependencies}))
    cases.append(_case(entity, "backend", "read_created", "administrator", "success", {"record": "created"}, depends_on=(f"backend:{entity.name}:create_valid:administrator",)))
    cases.append(_case(entity, "backend", "update_valid", "administrator", "success", {"record": "created", "changes": _update_payload(entity)}, depends_on=(f"backend:{entity.name}:create_valid:administrator",)))
    for field in entity.fields:
        # Layout directives are meaningful to Desk/frontend rendering but do
        # not persist a document value and therefore cannot have CRUD cases.
        if field.field_type in _LAYOUT_FIELD_TYPES:
            continue
        if field.required:
            cases.append(_case(entity, "backend", "create_missing_required", "administrator", "validation_error", {"omitted_field": field.name}))
        if field.unique:
            cases.append(_case(entity, "backend", "create_duplicate_unique", "administrator", "validation_error", {"field": field.name, "value": _sample_value(field)}))
        cases.append(_case(entity, "backend", "supported_field_type", "administrator", "success", {"field": field.name, "field_type": field.field_type.value, "value": _sample_value(field)}))
        if field.field_type is FieldType.SELECT:
            cases.append(_case(entity, "backend", "select_allowed", "administrator", "success", {"field": field.name, "value": field.options[0]}))
            cases.append(_case(entity, "backend", "select_rejected", "administrator", "validation_error", {"field": field.name, "value": "__unsupported_option__"}))
        if field.field_type is FieldType.LINK:
            target = field.options[0]
            cases.append(_case(entity, "backend", "link_valid", "administrator", "success", {"field": field.name, "target_entity": target, "fixture": _fixture_ref(target)}))
            cases.append(_case(entity, "backend", "link_missing", "administrator", "validation_error", {"field": field.name, "target_entity": target, "fixture": "__missing__"}))
    return cases


def _permission_cases(entity: EntitySpec) -> list[VerificationCase]:
    cases: list[VerificationCase] = []
    for permission in sorted(entity.permissions, key=lambda item: item.role):
        for action in _CRUD:
            cases.append(_case(entity, "permissions", action, permission.role, "allowed" if getattr(permission, action) else "denied", {"permission": action}))
    for action in _CRUD:
        cases.append(_case(entity, "permissions", action, "unassigned", "denied", {"permission": action}))
    return cases


def _rest_cases(entity: EntitySpec) -> list[VerificationCase]:
    cases: list[VerificationCase] = []
    for permission in sorted(entity.permissions, key=lambda item: item.role):
        for action, method in _REST:
            permission_name = {"list": "read", "read": "read", "update": "write"}.get(action, action)
            cases.append(_case(entity, "rest", action, permission.role, "success" if getattr(permission, permission_name) else "permission_denied", {"method": method, "permission": permission_name}))
    first_field = entity.fields[0]
    cases.extend((
        _case(entity, "rest", "list_filtered", "administrator", "success", {"method": "GET", "filter_field": first_field.name, "filter_operator": "equals", "filter_value": _sample_value(first_field)}),
        _case(entity, "rest", "list_ordered", "administrator", "success", {"method": "GET", "order_by": first_field.name, "order_direction": "asc"}),
        _case(entity, "rest", "list_paginated", "administrator", "success", {"method": "GET", "page_start": 0, "page_length": 2}),
        _case(entity, "rest", "invalid_payload", "administrator", "validation_error", {"method": "POST", "omitted_required_field": _required_field(entity)}),
        _case(entity, "rest", "normalized_validation_error", "administrator", "normalized_error", {"method": "POST", "omitted_required_field": _required_field(entity)}),
    ))
    return cases


def _anonymous_rest_cases(entities: tuple[EntitySpec, ...]) -> list[VerificationCase]:
    return [
        _case(entity, "rest", "list", "anonymous", "authentication_required", {"method": "GET"})
        for entity in sorted(entities, key=lambda item: item.name)
    ]


def _case(entity: EntitySpec, suite: str, operation: str, actor: str, expected: str, inputs: Mapping[str, Any], *, depends_on: tuple[str, ...] = ()) -> VerificationCase:
    unique_field = inputs.get("field", inputs.get("omitted_field"))
    suffix = f":{unique_field}" if isinstance(unique_field, str) else ""
    return VerificationCase(f"{suite}:{entity.name}:{operation}{suffix}:{actor}", suite, entity.name, operation, actor, expected, dict(inputs), depends_on)


def _valid_payload(entity: EntitySpec) -> dict[str, Any]:
    return {
        field.name: _sample_value(field)
        for field in entity.fields
        if field.field_type not in _LAYOUT_FIELD_TYPES and (field.required or field.default is not None)
    }


def _required_field(entity: EntitySpec) -> str:
    """Choose a deterministic safe invalidation for REST validation probes."""

    return next(field.name for field in entity.fields if field.required)


def _update_payload(entity: EntitySpec) -> dict[str, Any]:
    candidate = next(
        (
            field
            for field in entity.fields
            if field.field_type not in _LAYOUT_FIELD_TYPES | {FieldType.LINK, FieldType.CHECK}
        ),
        next(field for field in entity.fields if field.field_type not in _LAYOUT_FIELD_TYPES),
    )
    return {candidate.name: _update_value(candidate)}


def _fixture_ref(entity_name: str) -> dict[str, str]:
    return {"fixture_entity": entity_name, "fixture_name": f"_Test {entity_name}"}


def _sample_value(field: FieldSpec) -> Any:
    if field.default is not None:
        return field.default
    if field.field_type is FieldType.SELECT:
        return field.options[0]
    if field.field_type is FieldType.LINK:
        return _fixture_ref(field.options[0])
    values: dict[FieldType, Any] = {
        FieldType.DATA: f"_Test {field.name}", FieldType.EMAIL: "harness@example.invalid", FieldType.PHONE: "+15550100",
        FieldType.URL: "https://example.invalid/test", FieldType.SMALL_TEXT: f"_Test {field.name}", FieldType.LONG_TEXT: f"_Test {field.name}",
        FieldType.TEXT: f"_Test {field.name}", FieldType.TEXT_EDITOR: f"<p>_Test {field.name}</p>",
        FieldType.INT: 1, FieldType.FLOAT: 1.5, FieldType.CURRENCY: 1.5, FieldType.DATE: "2026-01-02", FieldType.DATETIME: "2026-01-02 03:04:05",
        FieldType.CHECK: True,
    }
    return values[field.field_type]


def _update_value(field: FieldSpec) -> Any:
    value = _sample_value(field)
    if isinstance(value, str):
        return value + " updated"
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return value + 1.0
    return value


def _summary(cases: list[VerificationCase], confirmed_spec_hash: str, template_hash: str) -> dict[str, Any]:
    by_suite: dict[str, int] = {}
    for case in cases:
        by_suite[case.suite] = by_suite.get(case.suite, 0) + 1
    return {
        "version": 1,
        "spec_hash": confirmed_spec_hash,
        "template_hash": template_hash,
        "case_count": len(cases),
        "cases_by_suite": dict(sorted(by_suite.items())),
        "execution": "not_executed",
    }
