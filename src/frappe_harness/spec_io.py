"""Strict JSON input/output helpers for canonical application specifications."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .contracts import (
    EntitySpec,
    FieldSpec,
    FieldType,
    PermissionSpec,
    ProjectSpec,
    RoleSpec,
    ScreenKind,
    ScreenSpec,
    canonical_data,
)


class SpecInputError(ValueError):
    """Raised when untrusted JSON cannot be converted into the typed contract."""


def load_project_spec(path: str | Path) -> ProjectSpec:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SpecInputError(f"could not read project specification: {error}") from error
    if not isinstance(raw, Mapping):
        raise SpecInputError("project specification must be a JSON object")
    return project_from_mapping(raw)


def project_from_mapping(raw: Mapping[str, Any]) -> ProjectSpec:
    try:
        roles = tuple(RoleSpec(str(item["name"]), str(item.get("description", ""))) for item in _array(raw, "roles"))
        entities = tuple(_entity(item) for item in _array(raw, "entities"))
        screens = tuple(_screen(item) for item in _array(raw, "screens"))
        return ProjectSpec(
            name=str(raw["name"]),
            label=str(raw["label"]),
            module=str(raw["module"]),
            entities=entities,
            roles=roles,
            screens=screens,
            version=int(raw.get("version", 1)),
            description=str(raw.get("description", "")),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SpecInputError(f"invalid project specification: {error}") from error


def dump_project_spec(spec: ProjectSpec) -> str:
    return json.dumps(canonical_data(spec), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _array(raw: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise SpecInputError(f"{key} must be an array of objects")
    return value


def _entity(raw: Mapping[str, Any]) -> EntitySpec:
    fields = tuple(_field(item) for item in _array(raw, "fields"))
    permissions = tuple(
        PermissionSpec(
            role=str(item["role"]),
            read=_bool(item, "read"),
            create=_bool(item, "create"),
            write=_bool(item, "write"),
            delete=_bool(item, "delete"),
        )
        for item in _array(raw, "permissions")
    )
    return EntitySpec(
        name=str(raw["name"]),
        label=str(raw["label"]),
        fields=fields,
        permissions=permissions,
        description=str(raw.get("description", "")),
        title_field=_optional_string(raw, "title_field"),
        default_sort_field=_optional_string(raw, "default_sort_field"),
        default_sort_order=_sort_order(raw),
        track_changes=_bool(raw, "track_changes", default=True),
        search_fields=_string_array(raw, "search_fields"),
        list_columns=_string_array(raw, "list_columns"),
        standard_filters=_string_array(raw, "standard_filters"),
    )


def _field(raw: Mapping[str, Any]) -> FieldSpec:
    options = raw.get("options", [])
    if not isinstance(options, list):
        raise SpecInputError("field options must be an array")
    return FieldSpec(
        name=str(raw["name"]),
        label=str(raw["label"]),
        field_type=FieldType(str(raw["field_type"])),
        required=_bool(raw, "required"),
        unique=_bool(raw, "unique"),
        default=raw.get("default"),
        options=tuple(str(item) for item in options),
        description=str(raw.get("description", "")),
        read_only=_bool(raw, "read_only"),
        hidden=_bool(raw, "hidden"),
        in_list_view=_bool(raw, "in_list_view"),
        in_standard_filter=_bool(raw, "in_standard_filter"),
        position=_non_negative_int(raw, "position"),
    )


def _screen(raw: Mapping[str, Any]) -> ScreenSpec:
    fields = raw.get("fields", [])
    if not isinstance(fields, list):
        raise SpecInputError("screen fields must be an array")
    return ScreenSpec(
        name=str(raw["name"]),
        entity=str(raw["entity"]),
        kind=ScreenKind(str(raw["kind"])),
        label=str(raw["label"]),
        fields=tuple(str(item) for item in fields),
    )


def _bool(raw: Mapping[str, Any], key: str, *, default: bool = False) -> bool:
    """Read a JSON boolean without coercing truthy strings or numbers."""

    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise SpecInputError(f"{key} must be a JSON boolean")
    return value


def _non_negative_int(raw: Mapping[str, Any], key: str) -> int:
    """Read a JSON non-negative integer without accepting booleans."""

    value = raw.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SpecInputError(f"{key} must be a non-negative JSON integer")
    return value


def _optional_string(raw: Mapping[str, Any], key: str) -> str | None:
    """Read an optional nullable JSON string used for field projections."""

    value = raw.get(key)
    if value is not None and not isinstance(value, str):
        raise SpecInputError(f"{key} must be a JSON string or null")
    return value


def _string_array(raw: Mapping[str, Any], key: str) -> tuple[str, ...]:
    """Read an optional JSON array containing strings only."""

    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SpecInputError(f"{key} must be an array of strings")
    return tuple(value)


def _sort_order(raw: Mapping[str, Any]) -> str:
    """Read the supported JSON sort-order literal without case coercion."""

    value = raw.get("default_sort_order", "asc")
    if value not in ("asc", "desc"):
        raise SpecInputError("default_sort_order must be 'asc' or 'desc'")
    return value
