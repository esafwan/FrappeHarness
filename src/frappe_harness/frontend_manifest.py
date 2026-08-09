"""Deterministic, data-only manifests for the fixed DocType-aware frontend.

The harness never generates page code.  This module projects an already
confirmed :class:`ProjectSpec` into a role-scoped allowlist that a fixed UI can
load.  Server-side Frappe permissions remain authoritative; the projection is
deliberately conservative so the client cannot discover an entity it cannot
read, submit arbitrary query fields, or show actions it is not permitted to
request.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any, Mapping

from .compiler import canonical_json
from .contracts import EntitySpec, FieldSpec, FieldType, ProjectSpec, ScreenKind, spec_hash, validate_project_spec


class FrontendManifestError(ValueError):
    """Raised when a confirmed specification cannot form a safe UI manifest."""


@dataclass(frozen=True)
class FrontendField:
    """A field the generic UI is allowed to render for one readable entity."""

    name: str
    label: str
    field_type: str
    required: bool
    unique: bool
    default: Any | None
    description: str
    component: str
    visible: bool = True
    read_only: bool = False
    select_options: tuple[str, ...] = ()
    link_target_entity: str | None = None
    link_target: str | None = None
    creatable: bool = False
    editable: bool = False


@dataclass(frozen=True)
class QueryField:
    """A list-screen query field and its fixed REST-safe operator allowlist."""

    name: str
    field_type: str
    operators: tuple[str, ...]


@dataclass(frozen=True)
class EntityActions:
    read: bool
    create: bool
    write: bool
    delete: bool


@dataclass(frozen=True)
class EntityManifest:
    name: str
    doctype: str
    label: str
    description: str
    fields: tuple[FrontendField, ...]
    actions: EntityActions


@dataclass(frozen=True)
class FrontendRoute:
    id: str
    kind: str
    entity: str
    label: str
    path: str
    fields: tuple[str, ...]
    allowed_filters: tuple[QueryField, ...] = ()
    allowed_orders: tuple[str, ...] = ()
    # Frappe's stable document key is necessary for a generic list to link to
    # an already-declared ``/:name`` form route.  It is deliberately separate
    # from display/filter fields: the fixed client may retrieve it for row
    # identity and navigation only, never expose it as an arbitrary query
    # field.
    record_id_field: str = "name"


@dataclass(frozen=True)
class NavigationItem:
    id: str
    label: str
    entity: str
    route_id: str
    path: str


@dataclass(frozen=True)
class RoleFrontendManifest:
    """Everything a fixed UI may expose for one declared Frappe role."""

    role: str
    entities: tuple[EntityManifest, ...]
    routes: tuple[FrontendRoute, ...]
    navigation: tuple[NavigationItem, ...]


@dataclass(frozen=True)
class FrontendManifest:
    version: int
    target_frappe_major: int
    project: str
    label: str
    spec_hash: str
    roles: tuple[RoleFrontendManifest, ...]
    manifest_hash: str


_TEXT_FILTERS = ("equals", "contains")
_EXACT_FILTERS = ("equals", "in")
_RANGE_FILTERS = ("equals", "lt", "lte", "gt", "gte", "between")
_FILTERS_BY_TYPE: Mapping[FieldType, tuple[str, ...]] = {
    FieldType.DATA: _TEXT_FILTERS,
    FieldType.EMAIL: _TEXT_FILTERS,
    FieldType.PHONE: _TEXT_FILTERS,
    FieldType.SMALL_TEXT: _TEXT_FILTERS,
    FieldType.LONG_TEXT: _TEXT_FILTERS,
    FieldType.TEXT: _TEXT_FILTERS,
    FieldType.TEXT_EDITOR: _TEXT_FILTERS,
    FieldType.URL: _TEXT_FILTERS,
    FieldType.SELECT: _EXACT_FILTERS,
    FieldType.LINK: _EXACT_FILTERS,
    FieldType.CHECK: ("equals",),
    FieldType.INT: _RANGE_FILTERS,
    FieldType.FLOAT: _RANGE_FILTERS,
    FieldType.CURRENCY: _RANGE_FILTERS,
    FieldType.DATE: _RANGE_FILTERS,
    FieldType.DATETIME: _RANGE_FILTERS,
}

# This is the complete, fixed V1 component vocabulary.  A runtime must use
# these stable identifiers rather than interpreting model- or spec-authored
# component names.  Layout fields are intentionally represented here because
# forms need to render their confirmed structure, while list projections reject
# them below.
COMPONENT_BY_FIELD_TYPE: Mapping[FieldType, str] = {
    FieldType.DATA: "text-input",
    FieldType.EMAIL: "email-input",
    FieldType.PHONE: "telephone-input",
    FieldType.URL: "url-input",
    FieldType.SMALL_TEXT: "textarea",
    FieldType.LONG_TEXT: "textarea",
    FieldType.TEXT: "textarea",
    FieldType.TEXT_EDITOR: "rich-text-editor",
    FieldType.INT: "integer-input",
    FieldType.FLOAT: "decimal-input",
    FieldType.CURRENCY: "currency-input",
    FieldType.CHECK: "checkbox",
    FieldType.DATE: "date-picker",
    FieldType.DATETIME: "datetime-picker",
    FieldType.SELECT: "select",
    FieldType.LINK: "link-autocomplete",
    FieldType.SECTION_BREAK: "form-section",
    FieldType.COLUMN_BREAK: "column-break",
}
_LAYOUT_TYPES = frozenset({FieldType.SECTION_BREAK, FieldType.COLUMN_BREAK})


def build_frontend_manifest(spec: ProjectSpec) -> FrontendManifest:
    """Build a stable per-role UI allowlist from one valid confirmed spec.

    A role only receives entities with ``read`` permission.  List and form
    routes are emitted only for readable entities and only from explicit
    ``ScreenSpec`` entries; there is no fallback route that could reveal a
    newly added entity without a confirmed screen.
    """

    report = validate_project_spec(spec)
    report.require_valid()
    role_manifests = tuple(_role_manifest(spec, role.name) for role in spec.roles)
    data = {
        "version": 1,
        "target_frappe_major": 16,
        "project": spec.name,
        "label": spec.label,
        "spec_hash": spec_hash(spec),
        "roles": [asdict(role) for role in role_manifests],
    }
    # The fixed browser loader hashes the compact canonical JSON string
    # without the renderer's artifact-terminating newline. Keep the manifest
    # identity byte-for-byte compatible across Python and TypeScript.
    manifest_hash = sha256(canonical_json(data).rstrip(b"\n")).hexdigest()
    return FrontendManifest(
        version=1,
        target_frappe_major=16,
        project=spec.name,
        label=spec.label,
        spec_hash=spec_hash(spec),
        roles=role_manifests,
        manifest_hash=manifest_hash,
    )


def frontend_manifest_json(manifest: FrontendManifest) -> str:
    """Serialize a manifest as canonical JSON suitable for an owned artifact."""

    return canonical_json(asdict(manifest)).decode("utf-8")


def _role_manifest(spec: ProjectSpec, role: str) -> RoleFrontendManifest:
    readable = tuple(
        entity for entity in spec.entities if _permission(entity, role).read
    )
    entity_by_name = {entity.name: entity for entity in spec.entities}
    entity_manifests = tuple(_entity_manifest(entity, role, entity_by_name) for entity in readable)
    readable_names = {entity.name for entity in readable}
    routes = tuple(
        _route(spec.name, screen, _entity_by_name(spec, screen.entity))
        for screen in spec.screens
        if screen.entity in readable_names
    )
    route_by_id = {route.id: route for route in routes}
    navigation = tuple(
        NavigationItem(screen.name, screen.label, screen.entity, screen.name, route_by_id[screen.name].path)
        for screen in spec.screens
        if screen.kind is ScreenKind.LIST and screen.name in route_by_id
    )
    return RoleFrontendManifest(role, entity_manifests, routes, navigation)


def _entity_by_name(spec: ProjectSpec, name: str) -> EntitySpec:
    return next(entity for entity in spec.entities if entity.name == name)


def _permission(entity: EntitySpec, role: str) -> Any:
    # Contract validation guarantees one row per declared role.
    return next(permission for permission in entity.permissions if permission.role == role)


def _entity_manifest(
    entity: EntitySpec, role: str, entity_by_name: Mapping[str, EntitySpec]
) -> EntityManifest:
    permission = _permission(entity, role)
    return EntityManifest(
        name=entity.name,
        doctype=entity.label,
        label=entity.label,
        description=entity.description,
        # Hidden fields are never disclosed to the fixed frontend.  Their
        # values may still be enforced by Frappe server-side defaults.
        fields=tuple(
            _field_manifest(field, permission.create, permission.write, entity_by_name)
            for field in entity.fields
            if not field.hidden
        ),
        actions=EntityActions(True, permission.create, permission.write, permission.delete),
    )


def _field_manifest(
    field: FieldSpec,
    creatable: bool,
    editable: bool,
    entity_by_name: Mapping[str, EntitySpec],
) -> FrontendField:
    try:
        component = COMPONENT_BY_FIELD_TYPE[field.field_type]
    except KeyError as error:  # defensive when the V1 field vocabulary grows
        raise FrontendManifestError(
            f"unsupported frontend component type: {field.field_type.value}"
        ) from error
    link_entity = field.options[0] if field.field_type is FieldType.LINK else None
    # Link options name an entity in the canonical spec.  A fixed frontend must
    # query Frappe's actual DocType label, never infer an endpoint from that
    # internal entity key.  Approved core DocTypes (currently User) retain
    # their explicit Frappe name.
    link_target = (
        entity_by_name[link_entity].label
        if link_entity in entity_by_name
        else link_entity
    )
    return FrontendField(
        name=field.name,
        label=field.label,
        field_type=field.field_type.value,
        required=field.required,
        unique=field.unique,
        default=field.default,
        description=field.description,
        component=component,
        visible=True,
        read_only=field.read_only,
        select_options=field.options if field.field_type is FieldType.SELECT else (),
        link_target_entity=link_entity,
        link_target=link_target,
        creatable=creatable and not field.read_only,
        editable=editable and not field.read_only,
    )


def _route(project: str, screen: Any, entity: EntitySpec) -> FrontendRoute:
    path = f"/{project}/{entity.name}"
    if screen.kind is ScreenKind.FORM:
        path += "/:name"
        return FrontendRoute(
            screen.name,
            screen.kind.value,
            entity.name,
            screen.label,
            path,
            tuple(field.name for field in _screen_fields(entity, screen.fields, screen.name)),
        )
    list_fields = _screen_fields(entity, screen.fields, screen.name, list_only=True)
    filter_names = _confirmed_filter_names(entity, list_fields)
    query_fields = tuple(_query_field(_field_by_name(entity, field_name)) for field_name in filter_names)
    # The same fixed list UI permits either direction only for its explicit
    # column allowlist.  Directions are part of the runtime contract, not
    # caller-provided strings in the manifest.
    return FrontendRoute(
        screen.name,
        screen.kind.value,
        entity.name,
        screen.label,
        path,
        tuple(field.name for field in list_fields),
        query_fields,
        tuple(f"{field.name}:{direction}" for field in list_fields for direction in ("asc", "desc")),
    )


def _field_by_name(entity: EntitySpec, name: str) -> FieldSpec:
    return next(field for field in entity.fields if field.name == name)


def _query_field(field: FieldSpec) -> QueryField:
    try:
        operators = _FILTERS_BY_TYPE[field.field_type]
    except KeyError as error:  # defensive when the V1 field vocabulary grows
        raise FrontendManifestError(f"unsupported frontend filter type: {field.field_type.value}") from error
    return QueryField(field.name, field.field_type.value, operators)


def _screen_fields(
    entity: EntitySpec, field_names: tuple[str, ...], screen_name: str, *, list_only: bool = False
) -> tuple[FieldSpec, ...]:
    fields = tuple(_field_by_name(entity, field_name) for field_name in field_names)
    for field in fields:
        if field.hidden:
            raise FrontendManifestError(
                f"screen {screen_name!r} references hidden field {field.name!r}"
            )
        if list_only and field.field_type in _LAYOUT_TYPES:
            raise FrontendManifestError(
                f"list screen {screen_name!r} references layout field {field.name!r}"
            )
    return fields


def _confirmed_filter_names(entity: EntitySpec, list_fields: tuple[FieldSpec, ...]) -> tuple[str, ...]:
    """Return only the explicitly confirmed standard-filter projection.

    Entity-level ``standard_filters`` is the strongest confirmation.  When it
    is omitted, the per-field ``in_standard_filter`` flag is the canonical
    fallback.  A list column alone never becomes an arbitrary query field.
    """

    list_names = {field.name for field in list_fields}
    declared = entity.standard_filters or tuple(
        field.name for field in entity.fields if field.in_standard_filter
    )
    names = tuple(name for name in declared if name in list_names)
    for name in names:
        field = _field_by_name(entity, name)
        if field.field_type in _LAYOUT_TYPES:
            raise FrontendManifestError(f"standard filter {name!r} is a layout field")
    return names
