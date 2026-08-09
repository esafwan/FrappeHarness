"""Pure, fail-closed boundary for untrusted structured project proposals.

This module accepts already-decoded JSON-like data only.  It deliberately has
no imports for model clients, files, subprocesses, Frappe, or networking.  A
proposal is useful only when it can become a validated :class:`ProjectSpec`;
free-form instructions, commands, and unknown properties are rejected rather
than interpreted.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
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
    ValidationIssue,
    validate_project_spec,
)


_PROJECT_KEYS = frozenset(
    {"name", "label", "module", "entities", "roles", "screens", "version", "description"}
)
_ROLE_KEYS = frozenset({"name", "description"})
_ENTITY_KEYS = frozenset(
    {
        "name", "label", "fields", "permissions", "description", "title_field",
        "default_sort_field", "default_sort_order", "track_changes", "search_fields",
        "list_columns", "standard_filters",
    }
)
_FIELD_KEYS = frozenset(
    {
        "name", "label", "field_type", "required", "unique", "default", "options",
        "description", "read_only", "hidden", "in_list_view", "in_standard_filter",
        "position",
    }
)
_PERMISSION_KEYS = frozenset({"role", "read", "create", "write", "delete"})
_SCREEN_KEYS = frozenset({"name", "entity", "kind", "label", "fields"})
_MAX_STRING_LENGTH = 2_000
_MAX_LIST_LENGTH = 100


@dataclass(frozen=True)
class ProposalIssue:
    """A deterministic parse or contract-validation diagnostic."""

    code: str
    path: str
    message: str


@dataclass(frozen=True)
class ClarificationRequest:
    """A question a caller may show before asking for another proposal."""

    path: str
    question: str


@dataclass(frozen=True)
class ProposalResult:
    """The sole successful output path to the trusted project contract."""

    spec: ProjectSpec | None
    issues: tuple[ProposalIssue, ...] = ()
    clarifications: tuple[ClarificationRequest, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.spec is not None and not self.issues

    def require_spec(self) -> ProjectSpec:
        if self.spec is None:
            details = "; ".join(f"{item.path}: {item.message}" for item in self.issues)
            raise ValueError(f"Proposal was not accepted: {details}")
        return self.spec


def parse_project_proposal(raw: object) -> ProposalResult:
    """Parse a JSON-shaped proposal without performing any external action.

    The input schema is intentionally the same small shape used for serialized
    ``ProjectSpec`` values.  Values are never coerced: for example ``"true"``
    cannot silently become a permission bit, and an object in ``default``
    cannot become an executable template.
    """

    issues: list[ProposalIssue] = []

    def error(code: str, path: str, message: str) -> None:
        issues.append(ProposalIssue(code, path, message))

    project = _object(raw, "project", _PROJECT_KEYS, error)
    if project is None:
        return _result(None, issues)

    roles = _roles(project.get("roles"), "project.roles", error)
    entities = _entities(project.get("entities"), "project.entities", error)
    screens = _screens(project.get("screens"), "project.screens", error)
    name = _required_string(project, "name", "project.name", error)
    label = _required_string(project, "label", "project.label", error)
    module = _required_string(project, "module", "project.module", error)
    description = _optional_string(project, "description", "project.description", error, default="")
    version = _optional_int(project, "version", "project.version", error, default=1)

    if issues:
        return _result(None, issues)
    assert name is not None and label is not None and module is not None
    assert description is not None and version is not None
    assert roles is not None and entities is not None and screens is not None
    spec = ProjectSpec(name, label, module, entities, roles, screens, version, description)
    validation = validate_project_spec(spec)
    for item in validation.errors:
        error("contract_" + item.code, item.path, item.message)
    return _result(spec if not issues else None, issues)


def _result(spec: ProjectSpec | None, issues: list[ProposalIssue]) -> ProposalResult:
    return ProposalResult(spec, tuple(issues), _clarifications(issues))


def _object(value: object, path: str, allowed: frozenset[str], error: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        error("expected_object", path, "must be a JSON object")
        return None
    for key in value:
        if not isinstance(key, str):
            error("invalid_key", path, "object keys must be strings")
        elif key not in allowed:
            error("unknown_property", f"{path}.{key}", "is not permitted in a project proposal")
    return value


def _array(value: object, path: str, error: Any) -> list[Any] | None:
    if not isinstance(value, list):
        error("expected_array", path, "must be a JSON array")
        return None
    if len(value) > _MAX_LIST_LENGTH:
        error("array_too_large", path, f"may contain at most {_MAX_LIST_LENGTH} items")
        return None
    return value


def _required_string(data: Mapping[str, Any], key: str, path: str, error: Any) -> str | None:
    if key not in data:
        error("missing_required", path, "is required")
        return None
    return _string(data[key], path, error)


def _optional_string(
    data: Mapping[str, Any], key: str, path: str, error: Any, *, default: str
) -> str | None:
    return default if key not in data else _string(data[key], path, error)


def _optional_nullable_string(
    data: Mapping[str, Any], key: str, path: str, error: Any
) -> str | None:
    if key not in data or data[key] is None:
        return None
    return _string(data[key], path, error)


def _string(value: object, path: str, error: Any) -> str | None:
    if not isinstance(value, str):
        error("expected_string", path, "must be a string; values are not coerced")
        return None
    if len(value) > _MAX_STRING_LENGTH:
        error("string_too_long", path, f"may contain at most {_MAX_STRING_LENGTH} characters")
        return None
    return value


def _optional_int(
    data: Mapping[str, Any], key: str, path: str, error: Any, *, default: int
) -> int | None:
    if key not in data:
        return default
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        error("expected_integer", path, "must be an integer; values are not coerced")
        return None
    return value


def _optional_bool(
    data: Mapping[str, Any], key: str, path: str, error: Any, *, default: bool = False
) -> bool | None:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, bool):
        error("expected_boolean", path, "must be true or false; values are not coerced")
        return None
    return value


def _roles(value: object, path: str, error: Any) -> tuple[RoleSpec, ...] | None:
    items = _array(value, path, error)
    if items is None:
        return None
    result: list[RoleSpec] = []
    for index, item in enumerate(items):
        data = _object(item, f"{path}[{index}]", _ROLE_KEYS, error)
        if data is None:
            continue
        name = _required_string(data, "name", f"{path}[{index}].name", error)
        description = _optional_string(
            data, "description", f"{path}[{index}].description", error, default=""
        )
        if name is not None and description is not None:
            result.append(RoleSpec(name, description))
    return tuple(result)


def _entities(value: object, path: str, error: Any) -> tuple[EntitySpec, ...] | None:
    items = _array(value, path, error)
    if items is None:
        return None
    result: list[EntitySpec] = []
    for index, item in enumerate(items):
        base = f"{path}[{index}]"
        data = _object(item, base, _ENTITY_KEYS, error)
        if data is None:
            continue
        name = _required_string(data, "name", base + ".name", error)
        label = _required_string(data, "label", base + ".label", error)
        fields = _fields(data.get("fields"), base + ".fields", error)
        permissions = _permissions(data.get("permissions"), base + ".permissions", error)
        description = _optional_string(
            data, "description", base + ".description", error, default=""
        )
        title_field = _optional_nullable_string(data, "title_field", base + ".title_field", error)
        default_sort_field = _optional_nullable_string(
            data, "default_sort_field", base + ".default_sort_field", error
        )
        default_sort_order = _optional_string(
            data, "default_sort_order", base + ".default_sort_order", error, default="asc"
        )
        track_changes = _optional_bool(
            data, "track_changes", base + ".track_changes", error, default=True
        )
        search_fields = _strings(data.get("search_fields", []), base + ".search_fields", error)
        list_columns = _strings(data.get("list_columns", []), base + ".list_columns", error)
        standard_filters = _strings(
            data.get("standard_filters", []), base + ".standard_filters", error
        )
        if None not in (
            name, label, fields, permissions, description, default_sort_order, track_changes,
            search_fields, list_columns, standard_filters,
        ):
            result.append(
                EntitySpec(
                    name, label, fields, permissions, description, title_field, default_sort_field,
                    default_sort_order, track_changes, search_fields, list_columns, standard_filters,
                )
            )
    return tuple(result)


def _fields(value: object, path: str, error: Any) -> tuple[FieldSpec, ...] | None:
    items = _array(value, path, error)
    if items is None:
        return None
    result: list[FieldSpec] = []
    for index, item in enumerate(items):
        base = f"{path}[{index}]"
        data = _object(item, base, _FIELD_KEYS, error)
        if data is None:
            continue
        name = _required_string(data, "name", base + ".name", error)
        label = _required_string(data, "label", base + ".label", error)
        field_type_text = _required_string(data, "field_type", base + ".field_type", error)
        try:
            field_type = FieldType(field_type_text) if field_type_text is not None else None
        except ValueError:
            error(
                "invalid_field_type",
                base + ".field_type",
                "must be one of the supported V1 field types",
            )
            field_type = None
        required = _optional_bool(data, "required", base + ".required", error)
        unique = _optional_bool(data, "unique", base + ".unique", error)
        options = _strings(data.get("options", []), base + ".options", error)
        default = (
            _default(data.get("default"), base + ".default", error)
            if "default" in data
            else None
        )
        description = _optional_string(
            data, "description", base + ".description", error, default=""
        )
        read_only = _optional_bool(data, "read_only", base + ".read_only", error)
        hidden = _optional_bool(data, "hidden", base + ".hidden", error)
        in_list_view = _optional_bool(data, "in_list_view", base + ".in_list_view", error)
        in_standard_filter = _optional_bool(
            data, "in_standard_filter", base + ".in_standard_filter", error
        )
        position = _optional_int(data, "position", base + ".position", error, default=0)
        if None not in (
            name, label, field_type, required, unique, options, description, read_only, hidden,
            in_list_view, in_standard_filter, position,
        ):
            result.append(
                FieldSpec(
                    name, label, field_type, required, unique, default, options, description,
                    read_only, hidden, in_list_view, in_standard_filter, position,
                )
            )
    return tuple(result)


def _strings(value: object, path: str, error: Any) -> tuple[str, ...] | None:
    items = _array(value, path, error)
    if items is None:
        return None
    result = [_string(item, f"{path}[{index}]", error) for index, item in enumerate(items)]
    if not all(item is not None for item in result):
        return None
    return tuple(item for item in result if item is not None)


def _default(value: object, path: str, error: Any) -> Any | None:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    error("invalid_default", path, "must be null, string, boolean, or finite number")
    return None


def _permissions(value: object, path: str, error: Any) -> tuple[PermissionSpec, ...] | None:
    items = _array(value, path, error)
    if items is None:
        return None
    result: list[PermissionSpec] = []
    for index, item in enumerate(items):
        base = f"{path}[{index}]"
        data = _object(item, base, _PERMISSION_KEYS, error)
        if data is None:
            continue
        role = _required_string(data, "role", base + ".role", error)
        bits = [
            _optional_bool(data, action, base + "." + action, error)
            for action in ("read", "create", "write", "delete")
        ]
        if role is not None and all(bit is not None for bit in bits):
            result.append(PermissionSpec(role, *bits))
    return tuple(result)


def _screens(value: object, path: str, error: Any) -> tuple[ScreenSpec, ...] | None:
    items = _array(value, path, error)
    if items is None:
        return None
    result: list[ScreenSpec] = []
    for index, item in enumerate(items):
        base = f"{path}[{index}]"
        data = _object(item, base, _SCREEN_KEYS, error)
        if data is None:
            continue
        name = _required_string(data, "name", base + ".name", error)
        entity = _required_string(data, "entity", base + ".entity", error)
        kind_text = _required_string(data, "kind", base + ".kind", error)
        try:
            kind = ScreenKind(kind_text) if kind_text is not None else None
        except ValueError:
            error("invalid_screen_kind", base + ".kind", "must be 'list' or 'form'")
            kind = None
        label = _required_string(data, "label", base + ".label", error)
        fields = _strings(data.get("fields", []), base + ".fields", error)
        if None not in (name, entity, kind, label, fields):
            result.append(ScreenSpec(name, entity, kind, label, fields))
    return tuple(result)


def _clarifications(issues: list[ProposalIssue]) -> tuple[ClarificationRequest, ...]:
    questions: list[ClarificationRequest] = []
    for issue in issues:
        if issue.code == "missing_required":
            questions.append(
                ClarificationRequest(issue.path, f"What value should be used for {issue.path}?")
            )
        elif issue.code == "contract_incomplete_permissions":
            questions.append(
                ClarificationRequest(
                    issue.path, "What CRUD permissions should each declared role have?"
                )
            )
        elif issue.code in {"contract_unknown_link_target", "contract_link_target_required"}:
            questions.append(
                ClarificationRequest(
                    issue.path, "Which declared entity should this Link field target?"
                )
            )
        elif issue.code == "contract_select_options_required":
            questions.append(
                ClarificationRequest(issue.path, "What are the allowed Select options?")
            )
    return tuple(questions)
