"""Deterministic renderer for the supported Frappe 16 metadata subset.

The compiler deliberately accepts only an already-confirmed ``ProjectSpec``.  It
does not inspect a Bench, write files, or invoke Frappe; callers persist the
returned in-memory artifacts only after their own validation/approval gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from enum import Enum
from typing import Any, Iterable, Mapping

from .contracts import FieldType, ProjectSpec, spec_hash as canonical_spec_hash
from .frappe_field_adapter import project_field_type


class CompilationError(ValueError):
    """Raised when a confirmed spec cannot be rendered safely."""


@dataclass(frozen=True)
class OwnershipEntry:
    """A compiler-owned file and its exact generated content digest."""

    path: str
    sha256: str


@dataclass(frozen=True)
class CompilationResult:
    """Stable, in-memory output of a single project-spec compilation."""

    artifacts: Mapping[str, bytes]
    ownership_manifest: Mapping[str, Any]
    spec_hash: str

    def text(self, path: str) -> str:
        return self.artifacts[path].decode("utf-8")


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
# The renderer must not admit field types beyond the typed V1 contract even
# when invoked through its backwards-compatible mapping boundary.  Otherwise
# an unvalidated legacy mapping could generate a field V1 explicitly defers.
_FIELD_TYPES = frozenset(field_type.value for field_type in FieldType)
_PERMISSION_KEYS = ("read", "write", "create", "delete", "submit", "cancel", "amend", "report", "export", "import", "share", "print", "email")


def canonical_json(value: Any) -> bytes:
    """Return canonical UTF-8 JSON suitable for hashes and generated JSON files."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"


def compile_project(spec: Any) -> CompilationResult:
    """Compile a confirmed ProjectSpec into deterministic Frappe 16 artifacts.

    Required object attributes are described by the project contracts: project_id,
    spec_version, app_slug, app_title, module_name, publisher, publisher_email,
    entities, and roles.  Nested values may be dataclasses or mappings.  This
    loose access boundary allows contracts to evolve without coupling rendering
    to a particular validation library.
    """

    app_slug = _identifier(_value(spec, "app_slug"), "app_slug")
    app_title = _text(_value(spec, "app_title"), "app_title")
    module_name = _text(_value(spec, "module_name"), "module_name")
    # Frappe resolves a standard DocType through the module package named by
    # ``DocType.module`` (after scrubbing that human-facing module name).  The
    # displayed module name is retained in JSON and modules.txt; its safe
    # import-path counterpart is used only for the package directory.
    module_slug = _slug(module_name)
    publisher = _text(_value(spec, "publisher", "Frappe Harness"), "publisher")
    publisher_email = _text(_value(spec, "publisher_email", "noreply@example.invalid"), "publisher_email")
    entities = _sorted_objects(_sequence(spec, "entities"), "doctype_name")
    roles = _sorted_objects(_sequence(spec, "roles"), "role_name")
    if not entities:
        raise CompilationError("ProjectSpec must contain at least one entity")

    doctype_names = {_text(_value(entity, "doctype_name"), "doctype_name") for entity in entities}
    doctype_by_entity = {_text(_value(entity, "entity_id"), "entity_id"): _text(_value(entity, "doctype_name"), "doctype_name") for entity in entities}
    # Every output path is rooted at the deployable app directory.  The Python
    # package is nested beneath that directory, as it is in a Frappe app
    # scaffold; fixtures and retained harness evidence intentionally are not.
    app_root = app_slug
    package_root = f"{app_root}/{app_slug}"
    harness_root = f"{app_root}/harness"
    artifacts: dict[str, bytes] = {}
    # These are real Python packages, not just directories that happen to
    # contain DocType metadata.  Frappe discovers standard DocTypes through
    # the importable app package hierarchy during migrations.
    # Frappe's app discovery imports the package and reads ``__version__``
    # while reporting installed apps.  This is declarative app metadata, not
    # executable generated logic; leaving the initializer empty breaks
    # ``bench --site … version`` after a compiler-owned update.
    artifacts[f"{package_root}/__init__.py"] = b'__version__ = "0.0.1"\n'
    artifacts[f"{package_root}/api.py"] = _frontend_api()
    module_root = f"{package_root}/{module_slug}"
    artifacts[f"{module_root}/__init__.py"] = b""
    artifacts[f"{module_root}/doctype/__init__.py"] = b""
    artifacts[f"{package_root}/hooks.py"] = _hooks(app_slug, app_title, publisher, publisher_email)
    artifacts[f"{package_root}/modules.txt"] = (module_name + "\n").encode("utf-8")
    artifacts[f"{app_root}/fixtures/roles.json"] = canonical_json([_render_role(role) for role in roles])

    for entity in entities:
        doctype_name = _text(_value(entity, "doctype_name"), "doctype_name")
        directory = _slug(doctype_name)
        # Frappe resolves standard DocTypes from
        # ``<app>/<python_package>/<module_slug>/doctype/<doctype>/<doctype>.json``.
        # Keep this real application layout in the preview so audited files
        # can be copied only by a future typed execution adapter without
        # relocation.
        prefix = f"{module_root}/doctype/{directory}"
        artifacts[f"{prefix}/__init__.py"] = b""
        artifacts[f"{prefix}/{directory}.json"] = canonical_json(
            _render_doctype(entity, module_name, doctype_names, doctype_by_entity, roles)
        )
        artifacts[f"{prefix}/{directory}.py"] = _controller(doctype_name)

    spec_snapshot = _plain(spec)
    spec_bytes = canonical_json(spec_snapshot)
    artifacts[f"{harness_root}/spec.json"] = spec_bytes
    # This hash is the confirmation identity. It must not be a renderer-only
    # projection that omits screens or other valid contract fields.
    spec_hash = canonical_spec_hash(spec)

    # Only the current, validated typed contract receives declarative runtime
    # verification artifacts.  The renderer intentionally still accepts the
    # legacy fixture-shaped objects used by older callers; attempting to infer
    # a typed verification plan from those loose objects would weaken the
    # contract and make their historical compiler output incompatible.
    if isinstance(spec, ProjectSpec):
        # Import lazily: test_templates uses canonical_json from this module.
        from .test_templates import generate_verification_templates
        from .frontend_manifest import build_frontend_manifest, frontend_manifest_json

        templates = generate_verification_templates(spec)
        if templates.spec_hash != spec_hash:
            raise CompilationError("verification templates do not bind to the confirmed specification hash")
        overlap = set(artifacts).intersection(templates.artifacts)
        if overlap:  # defensive: generated artifact ownership must be unique.
            raise CompilationError(f"verification template artifact collision: {', '.join(sorted(overlap))}")
        artifacts.update({f"{app_root}/{path}": content for path, content in templates.artifacts.items()})
        frontend_manifest = build_frontend_manifest(spec)
        if frontend_manifest.spec_hash != spec_hash:
            raise CompilationError("frontend manifest does not bind to the confirmed specification hash")
        artifacts[f"{harness_root}/frontend-manifest.json"] = frontend_manifest_json(frontend_manifest).encode("utf-8")

    # Hash artifact content before adding the manifest, avoiding a self-referential
    # manifest hash.  The manifest itself is still owned and hash-addressed by the
    # enclosing run record rather than its own entries.
    entries = [OwnershipEntry(path, sha256(content).hexdigest()) for path, content in sorted(artifacts.items())]
    manifest = {
        "manifest_version": 1,
        "app_slug": app_slug,
        "project_id": _text(_value(spec, "project_id"), "project_id"),
        "spec_hash": spec_hash,
        "owned_files": [{"path": entry.path, "sha256": entry.sha256} for entry in entries],
    }
    artifacts[f"{harness_root}/ownership-manifest.json"] = canonical_json(manifest)
    return CompilationResult(artifacts=dict(sorted(artifacts.items())), ownership_manifest=manifest, spec_hash=spec_hash)


def _render_doctype(entity: Any, module_name: str, doctype_names: set[str], doctype_by_entity: Mapping[str, str], roles: list[Any]) -> dict[str, Any]:
    name = _text(_value(entity, "doctype_name"), "doctype_name")
    fields = _sorted_fields(_sequence(entity, "fields"))
    rendered_fields = [_render_field(field, doctype_names, doctype_by_entity) for field in fields]
    search_fields = _string_list(_value(entity, "search_fields", ()), "search_fields")
    list_columns = _string_list(_value(entity, "list_columns", ()), "list_columns")
    standard_filters = _string_list(_value(entity, "standard_filters", ()), "standard_filters")
    known_fields = {field["fieldname"] for field in rendered_fields}
    for collection_name, values in (("search_fields", search_fields), ("list_columns", list_columns), ("standard_filters", standard_filters)):
        unknown = sorted(set(values) - known_fields)
        if unknown:
            raise CompilationError(f"{name}.{collection_name} references unknown fields: {', '.join(unknown)}")
    title_field = _value(entity, "title_field", None)
    if title_field is not None and title_field not in known_fields:
        raise CompilationError(f"{name}.title_field references unknown field {title_field!r}")

    return {
        "actions": [],
        "allow_copy": 0,
        "allow_rename": 0,
        "autoname": "hash",
        "creation": "",
        "doctype": "DocType",
        "editable_grid": 1,
        "engine": "InnoDB",
        "field_order": [field["fieldname"] for field in rendered_fields],
        "fields": rendered_fields,
        "is_submittable": 0,
        "istable": 0,
        "links": [],
        "module": module_name,
        "name": name,
        "naming_rule": "Random",
        "permissions": _permissions_for(name, roles, _value(entity, "permissions", ())),
        "search_fields": ", ".join(search_fields),
        "sort_field": _value(entity, "default_sort_field", "modified"),
        "sort_order": _value(entity, "default_sort_order", "DESC"),
        "states": [],
        "title_field": title_field or "",
        "track_changes": int(bool(_value(entity, "track_changes", False))),
    }


def _render_field(field: Any, doctype_names: set[str], doctype_by_entity: Mapping[str, str]) -> dict[str, Any]:
    fieldname = _identifier(_value(field, "fieldname"), "fieldname")
    semantic_type = _text(_value(field, "fieldtype"), f"{fieldname}.fieldtype")
    if semantic_type not in _FIELD_TYPES:
        raise CompilationError(f"Unsupported field type {semantic_type!r} for {fieldname}")
    fieldtype = project_field_type(FieldType(semantic_type)).fieldtype
    options = _value(field, "options", None)
    if fieldtype == "Link":
        if isinstance(options, (tuple, list)) and len(options) == 1:
            options = options[0]
        options = doctype_by_entity.get(options, options)
        if not isinstance(options, str) or options not in doctype_names:
            raise CompilationError(f"Link field {fieldname} must target a declared DocType")
    elif fieldtype == "Select":
        options = _select_options(options, fieldname)
    elif semantic_type == "URL":
        if options not in (None, "", (), []):
            raise CompilationError(f"URL field {fieldname} uses compiler-owned Frappe validation options")
        options = "URL"
    elif options not in (None, "", (), []):
        raise CompilationError(f"Only Link and Select fields may declare options ({fieldname})")
    rendered: dict[str, Any] = {
        "fieldname": fieldname,
        "fieldtype": fieldtype,
        "label": _text(_value(field, "label"), f"{fieldname}.label"),
        "reqd": int(bool(_value(field, "required", False))),
        "unique": int(bool(_value(field, "unique", False))),
        "read_only": int(bool(_value(field, "read_only", False))),
        "hidden": int(bool(_value(field, "hidden", False))),
        "in_list_view": int(bool(_value(field, "in_list_view", False))),
        "in_standard_filter": int(bool(_value(field, "in_standard_filter", False))),
    }
    default = _value(field, "default", None)
    if default is not None:
        rendered["default"] = default
    description = _value(field, "description", None)
    if description:
        rendered["description"] = _text(description, f"{fieldname}.description")
    if options:
        rendered["options"] = options
    return rendered


def _permissions_for(doctype_name: str, roles: Iterable[Any], entity_permissions: Iterable[Any]) -> list[dict[str, Any]]:
    direct = list(entity_permissions or ())
    if direct:
        rows = []
        for permission in sorted(direct, key=lambda item: _text(_value(item, "role"), "permission.role")):
            row = {"role": _text(_value(permission, "role"), "permission.role"), "permlevel": 0}
            row.update({key: int(bool(_value(permission, key, False))) for key in _PERMISSION_KEYS})
            rows.append(row)
        return rows
    rows: list[dict[str, Any]] = []
    for role in roles:
        role_name = _text(_value(role, "role_name"), "role_name")
        permissions = _value(role, "permissions", {})
        operations = _operations_for(permissions, doctype_name)
        if operations is not None:
            row = {"role": role_name, "permlevel": 0}
            row.update({key: int(bool(operations.get(key, False))) for key in _PERMISSION_KEYS})
            rows.append(row)
    return rows


def _operations_for(permissions: Any, doctype_name: str) -> Mapping[str, Any] | None:
    if isinstance(permissions, Mapping):
        found = permissions.get(doctype_name, permissions.get(_slug(doctype_name)))
        return found if isinstance(found, Mapping) else None
    for permission in permissions or ():
        target = _value(permission, "doctype_name", _value(permission, "entity", None))
        if target == doctype_name:
            return _value(permission, "operations", permission)
    return None


def _render_role(role: Any) -> dict[str, Any]:
    name = _text(_value(role, "role_name"), "role_name")
    return {"desk_access": 1, "disabled": 0, "doctype": "Role", "name": name, "role_name": name}


def _hooks(app_slug: str, app_title: str, publisher: str, email: str) -> bytes:
    lines = [
        f'app_name = {app_slug!r}', f'app_title = {app_title!r}', f'app_publisher = {publisher!r}',
        "app_description = 'Generated by Frappe Harness'", f'app_email = {email!r}', "app_license = 'MIT'",
        "required_apps = ['frappe']", "fixtures = ['Role']", "",
    ]
    return "\n".join(lines).encode("utf-8")


def _frontend_api() -> bytes:
    """Emit the fixed, role-bound manifest endpoint used by the Vue runtime."""
    return b'''"""Fixed frontend manifest endpoint generated by Frappe Harness."""

from __future__ import annotations

import json
from pathlib import Path

import frappe


_MANIFEST = Path(__file__).resolve().parents[1] / "harness" / "frontend-manifest.json"


@frappe.whitelist()
def frontend_manifest():
    """Return only the role projection allowed by the authenticated session."""
    if frappe.session.user == "Guest":
        frappe.throw("Authentication required", frappe.PermissionError)
    payload = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    roles = {item["role"]: item for item in payload["roles"]}
    effective_role = next((role for role in frappe.get_roles(frappe.session.user) if role in roles), None)
    if effective_role is None:
        frappe.throw("No frontend role is available for this user", frappe.PermissionError)
    return {"effective_role": effective_role, "manifest": payload}
'''


def _controller(doctype_name: str) -> bytes:
    class_name = re.sub(r"[^A-Za-z0-9]", "", doctype_name.title())
    return f'from frappe.model.document import Document\n\n\nclass {class_name}(Document):\n    pass\n'.encode("utf-8")


def _spec_snapshot(spec: Any, entities: list[Any], roles: list[Any]) -> dict[str, Any]:
    """Canonical projection of input data; excludes runtime-only target_site."""
    top = ("project_id", "spec_version", "app_slug", "app_title", "module_name", "publisher", "publisher_email")
    normalized_entities = []
    for entity in entities:
        plain = _plain(entity)
        plain["fields"] = sorted(
            plain.get("fields", []),
            key=lambda field: (int(field.get("position", 0)), field.get("fieldname", field.get("name", ""))),
        )
        normalized_entities.append(plain)
    return {
        **{key: _value(spec, key, None) for key in top},
        "entities": normalized_entities,
        "roles": [_plain(role) for role in roles],
    }


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return {name: _plain(getattr(value, name)) for name in value.__dataclass_fields__}
    raise CompilationError(f"Cannot canonicalize unsupported value {type(value).__name__}")


def _value(obj: Any, name: str, default: Any = ... ) -> Any:
    aliases = {
        "project_id": "name", "spec_version": "version", "app_slug": "name", "app_title": "label", "module_name": "module",
        "entity_id": "name", "doctype_name": "label", "role_name": "name", "fieldname": "name", "fieldtype": "field_type",
    }
    if isinstance(obj, Mapping) and name in obj:
        return obj[name]
    if hasattr(obj, name):
        return getattr(obj, name)
    alias = aliases.get(name)
    if alias:
        if isinstance(obj, Mapping) and alias in obj:
            return obj[alias]
        if hasattr(obj, alias):
            return getattr(obj, alias)
    if default is not ...:
        return default
    raise CompilationError(f"Missing required field {name}")


def _sequence(obj: Any, name: str) -> list[Any]:
    value = _value(obj, name)
    if not isinstance(value, (list, tuple)):
        raise CompilationError(f"{name} must be a list or tuple")
    return list(value)


def _sorted_objects(values: Iterable[Any], key: str) -> list[Any]:
    return sorted(values, key=lambda value: _text(_value(value, key), key))


def _sorted_fields(values: list[Any]) -> list[Any]:
    ordered = sorted(enumerate(values), key=lambda pair: (int(_value(pair[1], "position", pair[0])), _identifier(_value(pair[1], "fieldname"), "fieldname")))
    return [field for _, field in ordered]


def _string_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise CompilationError(f"{name} must be a list of strings")
    return list(value)


def _select_options(value: Any, fieldname: str) -> str:
    if isinstance(value, str):
        options = [part for part in value.split("\n") if part]
    elif isinstance(value, (list, tuple)) and all(isinstance(part, str) and part for part in value):
        options = list(value)
    else:
        raise CompilationError(f"Select field {fieldname} requires non-empty string options")
    if len(options) != len(set(options)):
        raise CompilationError(f"Select field {fieldname} has duplicate options")
    return "\n".join(options)


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise CompilationError(f"{name} must be a lowercase snake_case identifier")
    return value


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return _identifier(slug, "derived path")


def _text(value: Any, name: str) -> str:
    if isinstance(value, Enum):
        value = value.value
    if not isinstance(value, str) or not value.strip():
        raise CompilationError(f"{name} must be a non-empty string")
    return value
