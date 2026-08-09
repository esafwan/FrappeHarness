"""Canonical input contracts and static validation for the harness.

This module deliberately has no Frappe, filesystem, network, or execution
dependencies.  It is the narrow boundary between a confirmed project spec and
all later compiler/executor stages.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any, Mapping, Sequence


class FieldType(str, Enum):
    DATA = "Data"
    EMAIL = "Email"
    PHONE = "Phone"
    INT = "Int"
    FLOAT = "Float"
    CURRENCY = "Currency"
    DATE = "Date"
    DATETIME = "Datetime"
    CHECK = "Check"
    SELECT = "Select"
    LINK = "Link"
    SMALL_TEXT = "Small Text"
    LONG_TEXT = "Long Text"
    TEXT = "Text"
    TEXT_EDITOR = "Text Editor"
    URL = "URL"
    SECTION_BREAK = "Section Break"
    COLUMN_BREAK = "Column Break"


class ScreenKind(str, Enum):
    LIST = "list"
    FORM = "form"


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_ROLE_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]*$")
_EMAIL_DEFAULT = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_RESERVED_FIELD_NAMES = frozenset(
    {
        "amended_from", "creation", "docstatus", "doctype", "idx", "modified",
        "modified_by", "name", "owner", "parent", "parentfield", "parenttype",
    }
)
_LAYOUT_FIELD_TYPES = frozenset({FieldType.SECTION_BREAK, FieldType.COLUMN_BREAK})
_TEXT_FIELD_TYPES = frozenset(
    {
        FieldType.DATA, FieldType.EMAIL, FieldType.PHONE, FieldType.URL,
        FieldType.SMALL_TEXT, FieldType.LONG_TEXT, FieldType.TEXT,
        FieldType.TEXT_EDITOR,
    }
)
_SEARCHABLE_FIELD_TYPES = frozenset(
    {
        FieldType.DATA, FieldType.EMAIL, FieldType.PHONE, FieldType.SMALL_TEXT,
        FieldType.LONG_TEXT, FieldType.TEXT, FieldType.TEXT_EDITOR,
    }
)


def normalize_identifier(value: str) -> str:
    """Return a deterministic snake-case identifier suitable for a Frappe app.

    Normalization is intentionally not applied silently by validation: callers
    can present this result for human confirmation before constructing a spec.
    """

    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value.strip())
    value = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
    value = re.sub(r"_+", "_", value)
    if value and value[0].isdigit():
        value = "app_" + value
    return value


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str
    field_type: FieldType
    required: bool = False
    unique: bool = False
    default: Any | None = None
    options: tuple[str, ...] = ()
    description: str = ""
    read_only: bool = False
    hidden: bool = False
    in_list_view: bool = False
    in_standard_filter: bool = False
    # A concrete zero preserves the legacy compiler's ordered-field fallback.
    position: int = 0


@dataclass(frozen=True)
class RoleSpec:
    name: str
    description: str = ""


@dataclass(frozen=True)
class PermissionSpec:
    role: str
    read: bool = False
    create: bool = False
    write: bool = False
    delete: bool = False


@dataclass(frozen=True)
class EntitySpec:
    name: str
    label: str
    fields: tuple[FieldSpec, ...]
    permissions: tuple[PermissionSpec, ...]
    description: str = ""
    title_field: str | None = None
    default_sort_field: str | None = None
    default_sort_order: str = "asc"
    track_changes: bool = True
    search_fields: tuple[str, ...] = ()
    list_columns: tuple[str, ...] = ()
    standard_filters: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScreenSpec:
    name: str
    entity: str
    kind: ScreenKind
    label: str
    fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProjectSpec:
    name: str
    label: str
    module: str
    entities: tuple[EntitySpec, ...]
    roles: tuple[RoleSpec, ...]
    screens: tuple[ScreenSpec, ...]
    version: int = 1
    description: str = ""


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    path: str
    message: str
    severity: Severity = Severity.ERROR


@dataclass(frozen=True)
class ValidationReport:
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def valid(self) -> bool:
        return not any(issue.severity is Severity.ERROR for issue in self.issues)

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity is Severity.ERROR)

    def require_valid(self) -> None:
        if not self.valid:
            joined = "; ".join(f"{i.path}: {i.message}" for i in self.errors)
            raise ValueError(f"Invalid project specification: {joined}")


def canonical_data(value: Any) -> Any:
    """Convert contract values to JSON-safe stable data without reordering lists.

    Lists in a spec are semantically ordered (particularly Select options), so
    canonicalization preserves author-confirmed ordering and uses JSON key
    sorting for deterministic object output.
    """

    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return canonical_data(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): canonical_data(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [canonical_data(item) for item in value]
    return value


def canonical_json(spec: ProjectSpec) -> str:
    return json.dumps(canonical_data(spec), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def spec_hash(spec: ProjectSpec) -> str:
    return hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()


def validate_project_spec(spec: ProjectSpec) -> ValidationReport:
    issues: list[ValidationIssue] = []

    def error(code: str, path: str, message: str) -> None:
        issues.append(ValidationIssue(code, path, message))

    def warning(code: str, path: str, message: str) -> None:
        issues.append(ValidationIssue(code, path, message, Severity.WARNING))

    _validate_identifier(spec.name, "project.name", error)
    _validate_label(spec.label, "project.label", error)
    _validate_identifier(spec.module, "project.module", error)
    if spec.version < 1:
        error("invalid_version", "project.version", "version must be at least 1")
    if not 1 <= len(spec.entities) <= 5:
        error("entity_count", "project.entities", "V1 supports between 1 and 5 entities")

    entity_names = [entity.name for entity in spec.entities]
    _duplicates(entity_names, "project.entities", "duplicate_entity", error)
    role_names = [role.name for role in spec.roles]
    _duplicates(role_names, "project.roles", "duplicate_role", error)
    if not role_names:
        error("missing_roles", "project.roles", "at least one role is required")
    for index, role in enumerate(spec.roles):
        _validate_role(role.name, f"project.roles[{index}].name", error)

    known_entities = set(entity_names)
    known_roles = set(role_names)
    required_links: dict[str, set[str]] = {name: set() for name in known_entities}
    for entity_index, entity in enumerate(spec.entities):
        base = f"project.entities[{entity_index}]"
        _validate_identifier(entity.name, base + ".name", error)
        _validate_label(entity.label, base + ".label", error)
        if not isinstance(entity.track_changes, bool):
            error("invalid_track_changes", base + ".track_changes", "must be a boolean")
        if entity.default_sort_order not in ("asc", "desc"):
            error("invalid_sort_order", base + ".default_sort_order", "must be 'asc' or 'desc'")
        field_names = [item.name for item in entity.fields]
        _duplicates(field_names, base + ".fields", "duplicate_field", error)
        stored_fields = [item for item in entity.fields if item.field_type not in _LAYOUT_FIELD_TYPES]
        if len(stored_fields) > 20:
            error("field_count", base + ".fields", "V1 supports at most 20 stored fields per entity")
        for field_index, field_spec in enumerate(entity.fields):
            field_path = f"{base}.fields[{field_index}]"
            _validate_field(field_spec, field_path, known_entities, error)
            if field_spec.field_type is FieldType.LINK and field_spec.required and field_spec.options:
                required_links[entity.name].add(field_spec.options[0])
        permission_roles = [permission.role for permission in entity.permissions]
        _duplicates(permission_roles, base + ".permissions", "duplicate_permission", error)
        for permission_index, permission in enumerate(entity.permissions):
            permission_path = f"{base}.permissions[{permission_index}].role"
            if permission.role not in known_roles:
                error("unknown_role", permission_path, f"role {permission.role!r} is not declared")
        missing = known_roles - set(permission_roles)
        if missing:
            error("incomplete_permissions", base + ".permissions", "missing permission rows for: " + ", ".join(sorted(missing)))
        _validate_entity_field_references(entity, base, error)

    _validate_required_link_cycles(required_links, error)
    _validate_link_read_permissions(spec.entities, known_roles, warning)
    _validate_screens(spec.screens, known_entities, {e.name: e for e in spec.entities}, error)
    return ValidationReport(tuple(issues))

def _validate_identifier(value: str, path: str, error: Any) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        error("invalid_identifier", path, "must be lower snake_case, starting with a letter")


def _validate_role(value: str, path: str, error: Any) -> None:
    if not value or not _ROLE_IDENTIFIER.fullmatch(value):
        error("invalid_role", path, "must start with a letter and contain only letters, digits, spaces, _ or -")


def _validate_label(value: str, path: str, error: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        error("missing_label", path, "must not be blank")


def _duplicates(values: Sequence[str], path: str, code: str, error: Any) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            error(code, path, f"duplicate value {value!r}")
        seen.add(value)


def _validate_field(field_spec: FieldSpec, path: str, entities: set[str], error: Any) -> None:
    _validate_identifier(field_spec.name, path + ".name", error)
    _validate_label(field_spec.label, path + ".label", error)
    if not isinstance(field_spec.field_type, FieldType):
        error("unsupported_field_type", path + ".field_type", "must be one of the supported V1 field types")
        return
    if field_spec.name in _RESERVED_FIELD_NAMES:
        error("reserved_field_name", path + ".name", f"{field_spec.name!r} is reserved by Frappe")
    for property_name in ("required", "unique", "read_only", "hidden", "in_list_view", "in_standard_filter"):
        if not isinstance(getattr(field_spec, property_name), bool):
            error("invalid_boolean_property", path + f".{property_name}", "must be a boolean")
    if isinstance(field_spec.position, bool) or not isinstance(field_spec.position, int) or field_spec.position < 0:
        error("invalid_field_position", path + ".position", "must be a non-negative integer")
    if field_spec.required and field_spec.hidden and field_spec.default is None:
        error("required_hidden_without_default", path, "required hidden fields must have a default")
    if field_spec.field_type in _LAYOUT_FIELD_TYPES:
        if field_spec.required or field_spec.unique or field_spec.default is not None or field_spec.options:
            error("invalid_layout_field_property", path, "layout fields cannot be required, unique, defaulted, or configured with options")
        return
    if field_spec.field_type is FieldType.SELECT:
        if not field_spec.options:
            error("select_options_required", path + ".options", "Select fields require one or more options")
        normalized_options = []
        for option_index, option in enumerate(field_spec.options):
            if not isinstance(option, str) or not option.strip():
                error("invalid_select_option", path + f".options[{option_index}]", "must be a non-blank string")
                continue
            normalized_options.append(option.strip().casefold())
        _duplicates(normalized_options, path + ".options", "duplicate_select_option", error)
        if field_spec.default is not None and (not isinstance(field_spec.default, str) or field_spec.default not in field_spec.options):
            error("invalid_select_default", path + ".default", "default must be one of the Select options")
    elif field_spec.field_type is FieldType.LINK:
        if len(field_spec.options) != 1:
            error("link_target_required", path + ".options", "Link fields require exactly one target entity")
        elif field_spec.options[0] not in entities | {"User"}:
            error("unknown_link_target", path + ".options", f"target {field_spec.options[0]!r} is not a declared entity or approved core DocType")
        if field_spec.default is not None:
            error("link_default_unsupported", path + ".default", "Link defaults are unsupported in V1")
    elif field_spec.options:
        error("options_not_supported", path + ".options", "options are supported only for Select and Link fields")
    if field_spec.field_type is FieldType.CHECK and field_spec.default is not None and not isinstance(field_spec.default, bool):
        error("invalid_check_default", path + ".default", "Check default must be true or false")
    if field_spec.field_type is FieldType.INT and field_spec.default is not None and (isinstance(field_spec.default, bool) or not isinstance(field_spec.default, int)):
        error("invalid_int_default", path + ".default", "Int default must be an integer")
    if field_spec.field_type in (FieldType.FLOAT, FieldType.CURRENCY) and field_spec.default is not None and (isinstance(field_spec.default, bool) or not isinstance(field_spec.default, (int, float))):
        error("invalid_numeric_default", path + ".default", "numeric default must be a number")
    if field_spec.field_type in _TEXT_FIELD_TYPES and field_spec.default is not None and not isinstance(field_spec.default, str):
        error("invalid_string_default", path + ".default", "default must be a string")
    if field_spec.field_type is FieldType.EMAIL and isinstance(field_spec.default, str) and not _EMAIL_DEFAULT.fullmatch(field_spec.default):
        error("invalid_email_default", path + ".default", "default must be a valid email address")
    if field_spec.field_type is FieldType.PHONE and isinstance(field_spec.default, str) and not field_spec.default.strip():
        error("invalid_phone_default", path + ".default", "default must not be blank")


def _validate_entity_field_references(entity: EntitySpec, path: str, error: Any) -> None:
    """Validate safe entity projections against the ordered field declaration."""

    fields = {field_spec.name: field_spec for field_spec in entity.fields}

    def reference(field_name: str, collection: str, index: int, *, allow_hidden: bool = False) -> FieldSpec | None:
        item_path = f"{path}.{collection}[{index}]"
        field_spec = fields.get(field_name)
        if field_spec is None:
            error("unknown_entity_field", item_path, f"field {field_name!r} is not declared")
            return None
        if not allow_hidden and field_spec.hidden:
            error("hidden_entity_field", item_path, f"field {field_name!r} is hidden")
        if field_spec.field_type in _LAYOUT_FIELD_TYPES:
            error("layout_entity_field", item_path, f"field {field_name!r} is a layout field")
        return field_spec

    if entity.title_field is not None:
        title = reference(entity.title_field, "title_field", 0)
        if title is not None and title.field_type not in _TEXT_FIELD_TYPES:
            error("invalid_title_field", path + ".title_field", "title_field must reference a human-readable text field")
    if entity.default_sort_field is not None:
        reference(entity.default_sort_field, "default_sort_field", 0)
    for collection_name in ("list_columns", "standard_filters", "search_fields"):
        values = getattr(entity, collection_name)
        _duplicates(values, path + f".{collection_name}", f"duplicate_{collection_name}", error)
        for index, field_name in enumerate(values):
            field_spec = reference(field_name, collection_name, index)
            if collection_name == "search_fields" and field_spec is not None and field_spec.field_type not in _SEARCHABLE_FIELD_TYPES:
                error("unsuitable_search_field", f"{path}.{collection_name}[{index}]", f"field {field_name!r} is not a supported searchable field type")


def _validate_link_read_permissions(entities: Sequence[EntitySpec], roles: set[str], warning: Any) -> None:
    """Ensure a role which can select a generated Link can read its target."""

    by_name = {entity.name: entity for entity in entities}
    permission_maps = {
        entity.name: {permission.role: permission for permission in entity.permissions}
        for entity in entities
    }
    for entity_index, entity in enumerate(entities):
        for field_index, field_spec in enumerate(entity.fields):
            if field_spec.field_type is not FieldType.LINK or len(field_spec.options) != 1:
                continue
            target = field_spec.options[0]
            if target not in by_name:  # Approved core targets, including User, are checked by Frappe.
                continue
            for role in roles:
                source = permission_maps[entity.name].get(role)
                target_permission = permission_maps[target].get(role)
                if source is not None and (source.create or source.write) and (target_permission is None or not target_permission.read):
                    # Permission projections can intentionally hide an entity
                    # from a role in a frontend-only preview.  Preserve that
                    # compatibility while surfacing the unsafe Link as a
                    # preflight warning; the runtime verifier is the final
                    # enforcement point before a generated app is applied.
                    warning(
                        "link_target_not_readable",
                        f"project.entities[{entity_index}].fields[{field_index}].options",
                        f"role {role!r} can select Link target {target!r} but cannot read it",
                    )


def _validate_required_link_cycles(graph: Mapping[str, set[str]], error: Any) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, trail: list[str]) -> None:
        if node in visiting:
            cycle = trail[trail.index(node):] + [node]
            error("required_link_cycle", "project.entities", "required Link cycle: " + " -> ".join(cycle))
            return
        if node in visited:
            return
        visiting.add(node)
        for target in sorted(graph.get(node, ())):
            visit(target, trail + [target])
        visiting.remove(node)
        visited.add(node)

    for node in sorted(graph):
        visit(node, [node])


def _validate_screens(screens: Sequence[ScreenSpec], entities: set[str], entity_map: Mapping[str, EntitySpec], error: Any) -> None:
    names = [screen.name for screen in screens]
    _duplicates(names, "project.screens", "duplicate_screen", error)
    for index, screen in enumerate(screens):
        path = f"project.screens[{index}]"
        _validate_identifier(screen.name, path + ".name", error)
        _validate_label(screen.label, path + ".label", error)
        if screen.entity not in entities:
            error("unknown_screen_entity", path + ".entity", f"entity {screen.entity!r} is not declared")
            continue
        fields = {field_spec.name for field_spec in entity_map[screen.entity].fields}
        for field_name in screen.fields:
            if field_name not in fields:
                error("unknown_screen_field", path + ".fields", f"field {field_name!r} is not on {screen.entity!r}")
