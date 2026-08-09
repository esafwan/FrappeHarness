"""Conservative V1 classification of installed and confirmed specifications.

This is a *data-safety* classifier, not a provider or migration executor.  It
is deliberately closed-world: a change is blocked unless it is one of the few
changes V1 can show is data preserving.  Callers must still perform their
backup, checkpoint, ownership, approval, and post-migration gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .contracts import EntitySpec, FieldSpec, ProjectSpec, spec_hash, validate_project_spec


class ChangeDisposition(str, Enum):
    """The migration safety classification for one semantic change."""

    UNCHANGED = "unchanged"
    SAFE_ADDITIVE = "safe_additive"
    SAFE_UPDATE = "safe_update"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class SchemaChange:
    """One explicit, reviewable installed-to-confirmed difference."""

    code: str
    path: str
    disposition: ChangeDisposition
    installed: Any
    confirmed: Any
    reason: str


@dataclass(frozen=True)
class SchemaDiffReport:
    """An ordered, deterministic change report suitable for a migration gate."""

    changes: tuple[SchemaChange, ...] = ()
    installed_spec_hash: str | None = None
    confirmed_spec_hash: str | None = None

    @property
    def blocked(self) -> tuple[SchemaChange, ...]:
        return tuple(change for change in self.changes if change.disposition is ChangeDisposition.BLOCKED)

    @property
    def safe(self) -> bool:
        """Whether the diff is data-safe under the deliberately narrow V1 rules."""

        return not self.blocked

    @property
    def has_changes(self) -> bool:
        return bool(self.changes)


def classify_schema_diff(installed: ProjectSpec, confirmed: ProjectSpec) -> SchemaDiffReport:
    """Classify a confirmed spec against the schema currently installed.

    Both inputs are typed canonical contracts.  Invalid input is represented as
    a blocked finding rather than silently classified.  Existing metadata
    should first be normalized into this contract by a trusted observer; this
    function deliberately does not query Frappe or execute a provider.

    Safe V1 changes are limited to adding a DocType, adding a non-unique
    optional field, adding a non-unique required field with a non-Link default,
    and cosmetic/project-screen updates.  In particular, all changes to
    requiredness, uniqueness, defaults, Select choices, Link targets, and CRUD
    rows remain blocked until a provider-specific migration has evidence for
    them.  This implements DEC-06's fail-closed policy.
    """

    changes: list[SchemaChange] = []
    _append_invalid_spec(changes, "installed", installed)
    _append_invalid_spec(changes, "confirmed", confirmed)
    if changes:
        return SchemaDiffReport(tuple(changes), spec_hash(installed), spec_hash(confirmed))

    _compare_project_identity(installed, confirmed, changes)
    _compare_roles(installed, confirmed, changes)

    old_entities = {entity.name: entity for entity in installed.entities}
    new_entities = {entity.name: entity for entity in confirmed.entities}
    for name in sorted(old_entities.keys() - new_entities.keys()):
        _blocked(changes, "entity_removed", f"entities.{name}", old_entities[name], None, "removing a DocType can destroy records")
    for name in sorted(new_entities.keys() - old_entities.keys()):
        _safe_additive(changes, "entity_added", f"entities.{name}", None, new_entities[name], "a new DocType does not alter existing records")
    for name in sorted(old_entities.keys() & new_entities.keys()):
        _compare_entity(old_entities[name], new_entities[name], changes)

    _compare_screens(installed, confirmed, changes)
    return SchemaDiffReport(tuple(changes), spec_hash(installed), spec_hash(confirmed))


def _append_invalid_spec(changes: list[SchemaChange], side: str, spec: ProjectSpec) -> None:
    report = validate_project_spec(spec)
    for issue in report.errors:
        _blocked(changes, "invalid_spec", f"{side}.{issue.path}", None, issue.code, f"{issue.code}: {issue.message}")


def _compare_project_identity(old: ProjectSpec, new: ProjectSpec, changes: list[SchemaChange]) -> None:
    _compare_value(changes, "project_name", "project.name", old.name, new.name, ChangeDisposition.BLOCKED, "changing the app identity is outside a schema migration")
    _compare_value(changes, "project_module", "project.module", old.module, new.module, ChangeDisposition.BLOCKED, "changing the module moves provider-owned metadata")
    _compare_value(changes, "project_label", "project.label", old.label, new.label, ChangeDisposition.SAFE_UPDATE, "project labels do not change stored records")
    _compare_value(changes, "project_description", "project.description", old.description, new.description, ChangeDisposition.SAFE_UPDATE, "project descriptions do not change stored records")


def _compare_roles(old: ProjectSpec, new: ProjectSpec, changes: list[SchemaChange]) -> None:
    old_roles = {role.name: role for role in old.roles}
    new_roles = {role.name: role for role in new.roles}
    for name in sorted(old_roles.keys() - new_roles.keys()):
        _blocked(changes, "role_removed", f"roles.{name}", old_roles[name], None, "removing a role changes access control")
    for name in sorted(new_roles.keys() - old_roles.keys()):
        _blocked(changes, "role_added", f"roles.{name}", None, new_roles[name], "new roles require an access-control review")
    for name in sorted(old_roles.keys() & new_roles.keys()):
        _compare_value(changes, "role_description", f"roles.{name}.description", old_roles[name].description, new_roles[name].description, ChangeDisposition.SAFE_UPDATE, "role descriptions do not change access control")


def _compare_entity(old: EntitySpec, new: EntitySpec, changes: list[SchemaChange]) -> None:
    root = f"entities.{old.name}"
    # Provider plans use the entity label as the Frappe DocType name, so this
    # is not merely cosmetic for the current V1 provider boundary.
    _compare_value(changes, "entity_label", root + ".label", old.label, new.label, ChangeDisposition.BLOCKED, "changing a DocType label can rename provider metadata")
    _compare_value(changes, "entity_description", root + ".description", old.description, new.description, ChangeDisposition.SAFE_UPDATE, "entity descriptions do not change stored records")

    old_fields = {field.name: field for field in old.fields}
    new_fields = {field.name: field for field in new.fields}
    for name in sorted(old_fields.keys() - new_fields.keys()):
        _blocked(changes, "field_removed", root + f".fields.{name}", old_fields[name], None, "removing a field can destroy stored values")
    for name in sorted(new_fields.keys() - old_fields.keys()):
        _classify_field_addition(changes, root + f".fields.{name}", new_fields[name])
    for name in sorted(old_fields.keys() & new_fields.keys()):
        _compare_field(old_fields[name], new_fields[name], root + f".fields.{name}", changes)

    _compare_permissions(old, new, root, changes)


def _classify_field_addition(changes: list[SchemaChange], path: str, field: FieldSpec) -> None:
    if field.unique:
        _blocked(changes, "unique_field_added", path, None, field, "a unique field needs a data-specific backfill and collision check")
    elif not field.required:
        _safe_additive(changes, "optional_field_added", path, None, field, "an optional non-unique field preserves existing records")
    elif field.default is None:
        _blocked(changes, "required_field_without_default", path, None, field, "a new required field would invalidate existing records")
    elif field.field_type.value == "Link":
        _blocked(changes, "required_link_field_added", path, None, field, "Link defaults are unsupported and need a data-specific backfill")
    else:
        _safe_additive(changes, "required_field_with_default_added", path, None, field, "a validated non-Link default can populate existing records")


def _compare_field(old: FieldSpec, new: FieldSpec, path: str, changes: list[SchemaChange]) -> None:
    _compare_value(changes, "field_label", path + ".label", old.label, new.label, ChangeDisposition.SAFE_UPDATE, "field labels do not change stored values")
    # All of the following are intentionally blocked, including relaxing a
    # constraint. V1 has no provider-specific proof that metadata semantics,
    # indexes, and existing values remain equivalent after such a change.
    for code, attr, reason in (
        ("field_type", "field_type", "changing a field type can reinterpret or lose stored values"),
        ("field_required", "required", "changing requiredness needs an existing-data audit"),
        ("field_unique", "unique", "changing uniqueness needs an index and duplicate-value audit"),
        ("field_default", "default", "changing a default has provider-specific migration semantics"),
        ("field_options", "options", "changing Select choices or Link targets needs a data-specific migration"),
    ):
        _compare_value(changes, code, path + f".{attr}", getattr(old, attr), getattr(new, attr), ChangeDisposition.BLOCKED, reason)
    _compare_value(changes, "field_description", path + ".description", old.description, new.description, ChangeDisposition.SAFE_UPDATE, "field descriptions do not change stored values")


def _compare_permissions(old: EntitySpec, new: EntitySpec, root: str, changes: list[SchemaChange]) -> None:
    old_permissions = {permission.role: permission for permission in old.permissions}
    new_permissions = {permission.role: permission for permission in new.permissions}
    for role in sorted(old_permissions.keys() | new_permissions.keys()):
        old_permission = old_permissions.get(role)
        new_permission = new_permissions.get(role)
        if old_permission != new_permission:
            _blocked(changes, "permission_changed", root + f".permissions.{role}", old_permission, new_permission, "CRUD permission changes require a separate authorization review")


def _compare_screens(old: ProjectSpec, new: ProjectSpec, changes: list[SchemaChange]) -> None:
    old_screens = {screen.name: screen for screen in old.screens}
    new_screens = {screen.name: screen for screen in new.screens}
    for name in sorted(old_screens.keys() - new_screens.keys()):
        _safe_update(changes, "screen_removed", f"screens.{name}", old_screens[name], None, "screen configuration does not alter stored records")
    for name in sorted(new_screens.keys() - old_screens.keys()):
        _safe_update(changes, "screen_added", f"screens.{name}", None, new_screens[name], "screen configuration does not alter stored records")
    for name in sorted(old_screens.keys() & new_screens.keys()):
        _compare_value(changes, "screen_changed", f"screens.{name}", old_screens[name], new_screens[name], ChangeDisposition.SAFE_UPDATE, "screen configuration does not alter stored records")


def _compare_value(
    changes: list[SchemaChange], code: str, path: str, installed: Any, confirmed: Any,
    disposition: ChangeDisposition, reason: str,
) -> None:
    if installed != confirmed:
        changes.append(SchemaChange(code, path, disposition, installed, confirmed, reason))


def _blocked(changes: list[SchemaChange], code: str, path: str, installed: Any, confirmed: Any, reason: str) -> None:
    changes.append(SchemaChange(code, path, ChangeDisposition.BLOCKED, installed, confirmed, reason))


def _safe_additive(changes: list[SchemaChange], code: str, path: str, installed: Any, confirmed: Any, reason: str) -> None:
    changes.append(SchemaChange(code, path, ChangeDisposition.SAFE_ADDITIVE, installed, confirmed, reason))


def _safe_update(changes: list[SchemaChange], code: str, path: str, installed: Any, confirmed: Any, reason: str) -> None:
    changes.append(SchemaChange(code, path, ChangeDisposition.SAFE_UPDATE, installed, confirmed, reason))
