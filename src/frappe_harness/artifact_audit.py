"""Pure, fail-closed audit of compiler-owned preview artifacts.

The compiler returns bytes rather than writing a Bench.  This module validates
that byte set before it is persisted or used as evidence for a later provider
operation.  It deliberately has no filesystem, process, or Frappe dependency.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import PurePosixPath
import re
from typing import Any, Mapping

from .compiler import CompilationResult, canonical_json
from .frontend_manifest import COMPONENT_BY_FIELD_TYPE
from .verification_program import VerificationProgramError, load_verification_program


_HASH = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*['\"][^'\"]{8,}"),
)
_SUPPORTED_HOOKS = frozenset({
    "app_name", "app_title", "app_publisher", "app_description", "app_email",
    "app_license", "required_apps", "fixtures",
})


@dataclass(frozen=True)
class ArtifactAuditIssue:
    code: str
    path: str
    message: str


@dataclass(frozen=True)
class ArtifactAuditReport:
    issues: tuple[ArtifactAuditIssue, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.issues


def audit_compilation(result: CompilationResult) -> ArtifactAuditReport:
    """Audit one compiler result, including its in-memory manifest projection."""

    return audit_artifacts(
        result.artifacts,
        result.ownership_manifest,
        expected_spec_hash=result.spec_hash,
    )


def audit_artifacts(
    artifacts: Mapping[str, bytes],
    ownership_manifest: Mapping[str, Any] | None = None,
    *,
    expected_spec_hash: str | None = None,
) -> ArtifactAuditReport:
    """Validate artifact structure, bytes, ownership hashes, and safe Python.

    The caller must pass the manifest decoded from a trusted compiler result or
    disk.  The serialized manifest artifact is checked independently so the two
    cannot silently diverge.
    """

    issues: list[ArtifactAuditIssue] = []
    supplied_app_slug = _manifest_app_slug(ownership_manifest)
    manifest_from_file = _load_manifest(artifacts, issues, expected_path=_manifest_path(supplied_app_slug) if supplied_app_slug else None)
    manifest = ownership_manifest if ownership_manifest is not None else manifest_from_file
    app_slug = _manifest_app_slug(manifest)
    manifest_path = _manifest_path(app_slug) if app_slug else "<app>/harness/ownership-manifest.json"
    if ownership_manifest is not None and manifest_from_file is not None:
        if dict(ownership_manifest) != dict(manifest_from_file):
            _issue(issues, "manifest_mismatch", manifest_path, "serialized manifest differs from supplied manifest")

    for path, content in artifacts.items():
        _audit_path(path, issues)
        if not isinstance(content, bytes):
            _issue(issues, "non_bytes_artifact", path, "artifact content must be bytes")
            continue
        _audit_secrets(path, content, issues)
        if path.endswith(".json"):
            _audit_json(path, content, issues, app_slug=app_slug)
        elif path.endswith(".py"):
            _audit_python(path, content, issues, app_slug=app_slug)

    _audit_required_structure(artifacts, manifest, issues)
    _audit_ownership(artifacts, manifest, expected_spec_hash, issues)
    return ArtifactAuditReport(tuple(issues))


def _manifest_path(app_slug: str) -> str:
    return f"{app_slug}/harness/ownership-manifest.json"


def _manifest_app_slug(manifest: Mapping[str, Any] | None) -> str | None:
    if not isinstance(manifest, Mapping):
        return None
    app_slug = manifest.get("app_slug")
    return app_slug if isinstance(app_slug, str) and _IDENTIFIER.fullmatch(app_slug) else None


def _load_manifest(
    artifacts: Mapping[str, bytes], issues: list[ArtifactAuditIssue], *, expected_path: str | None,
) -> Mapping[str, Any] | None:
    candidates = [expected_path] if expected_path else [
        path for path in artifacts
        if isinstance(path, str) and re.fullmatch(r"[a-z][a-z0-9_]*/harness/ownership-manifest\.json", path)
    ]
    if len(candidates) != 1:
        _issue(issues, "missing_manifest", expected_path or "<app>/harness/ownership-manifest.json", "ownership manifest artifact is required")
        return None
    manifest_path = candidates[0]
    content = artifacts.get(manifest_path)
    if content is None:
        _issue(issues, "missing_manifest", manifest_path, "ownership manifest artifact is required")
        return None
    if not isinstance(content, bytes):
        return None
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None  # JSON audit emits the user-facing issue.
    if not isinstance(value, Mapping):
        _issue(issues, "invalid_manifest", manifest_path, "manifest must be a JSON object")
        return None
    if content != canonical_json(value):
        _issue(issues, "noncanonical_json", manifest_path, "manifest must use canonical JSON encoding")
    return value


def _audit_path(path: Any, issues: list[ArtifactAuditIssue]) -> None:
    if not isinstance(path, str) or not path:
        _issue(issues, "invalid_path", str(path), "artifact path must be a non-empty string")
        return
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or ".." in candidate.parts or path != candidate.as_posix() or "\\" in path:
        _issue(issues, "unsafe_path", path, "artifact path must be a normalized relative POSIX path")


def _audit_secrets(path: str, content: bytes, issues: list[ArtifactAuditIssue]) -> None:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        _issue(issues, "non_utf8_artifact", path, "generated artifacts must be UTF-8")
        return
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        _issue(issues, "secret_detected", path, "artifact appears to contain a credential or private key")


def _audit_json(path: str, content: bytes, issues: list[ArtifactAuditIssue], *, app_slug: str | None) -> None:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        _issue(issues, "invalid_json", path, f"artifact is not valid UTF-8 JSON: {error}")
        return
    if content != canonical_json(value):
        _issue(issues, "noncanonical_json", path, "JSON artifact must use canonical UTF-8 encoding")
    relative_path = _app_relative(path, app_slug)
    if relative_path.endswith(".json") and "/doctype/" in relative_path:
        _audit_doctype_json(path, value, issues)
    elif relative_path == "fixtures/roles.json":
        if not isinstance(value, list) or not all(isinstance(role, Mapping) and isinstance(role.get("role_name"), str) for role in value):
            _issue(issues, "invalid_roles_fixture", path, "roles fixture must be a list of Role objects with role_name")
    elif relative_path == "harness/spec.json" and not isinstance(value, Mapping):
        _issue(issues, "invalid_spec_snapshot", path, "spec snapshot must be a JSON object")
    elif relative_path == "harness/frontend-manifest.json":
        _audit_frontend_manifest(path, value, issues)
    elif relative_path == "harness/verification-plan.json":
        _audit_verification_plan(path, value, issues)
    elif relative_path == "harness/verification-summary.json":
        _audit_verification_summary(path, value, issues)
    elif relative_path.startswith("harness/tests/"):
        _audit_entity_verification_cases(path, relative_path, value, issues)


def _audit_frontend_manifest(path: str, value: Any, issues: list[ArtifactAuditIssue]) -> None:
    """Audit the compiler-owned, data-only fixed-frontend projection."""

    if not isinstance(value, Mapping):
        _issue(issues, "invalid_frontend_manifest", path, "frontend manifest must be a JSON object")
        return
    if value.get("version") != 1 or value.get("target_frappe_major") != 16:
        _issue(issues, "invalid_frontend_manifest", path, "manifest must target frontend version 1 and Frappe major 16")
    for key in ("project", "label", "spec_hash", "manifest_hash"):
        item = value.get(key)
        if not isinstance(item, str) or not item:
            _issue(issues, "invalid_frontend_manifest", path, f"{key} must be a non-empty string")
    for key in ("spec_hash", "manifest_hash"):
        item = value.get(key)
        if isinstance(item, str) and not _HASH.fullmatch(item):
            _issue(issues, "invalid_frontend_manifest", path, f"{key} must be a SHA-256 hex digest")
    expected_hash_data = dict(value)
    manifest_hash = expected_hash_data.pop("manifest_hash", None)
    if isinstance(manifest_hash, str) and _HASH.fullmatch(manifest_hash):
        expected_hash = sha256(canonical_json(expected_hash_data).rstrip(b"\n")).hexdigest()
        if manifest_hash != expected_hash:
            _issue(issues, "frontend_manifest_hash_mismatch", path, "manifest_hash does not bind manifest content")
    roles = value.get("roles")
    if not isinstance(roles, list) or not all(isinstance(role, Mapping) and isinstance(role.get("role"), str) for role in roles):
        _issue(issues, "invalid_frontend_manifest", path, "roles must be a list of role manifests")
        return
    component_names = set(COMPONENT_BY_FIELD_TYPE.values())
    for role in roles:
        entities = role.get("entities")
        routes = role.get("routes")
        navigation = role.get("navigation")
        if not isinstance(entities, list) or not isinstance(routes, list) or not isinstance(navigation, list):
            _issue(issues, "invalid_frontend_manifest", path, "role manifests require entities, routes, and navigation arrays")
            continue
        entity_names = {entity.get("name") for entity in entities if isinstance(entity, Mapping)}
        for entity in entities:
            if not isinstance(entity, Mapping) or not isinstance(entity.get("actions"), Mapping):
                _issue(issues, "invalid_frontend_manifest", path, "entity manifests require actions")
                continue
            fields = entity.get("fields")
            if not isinstance(fields, list):
                _issue(issues, "invalid_frontend_manifest", path, "entity manifest fields must be an array")
                continue
            for field in fields:
                if not isinstance(field, Mapping):
                    _issue(issues, "invalid_frontend_manifest", path, "frontend fields must be objects")
                    continue
                if field.get("visible") is not True or field.get("component") not in component_names:
                    _issue(issues, "invalid_frontend_manifest", path, "fields require a visible approved component")
                if field.get("field_type") == "Link":
                    if not isinstance(field.get("link_target"), str) or not isinstance(field.get("link_target_entity"), str):
                        _issue(issues, "invalid_frontend_manifest", path, "Link fields require entity and DocType targets")
        route_ids = set()
        for route in routes:
            if not isinstance(route, Mapping) or route.get("entity") not in entity_names:
                _issue(issues, "invalid_frontend_manifest", path, "routes must target a role-visible entity")
                continue
            route_id = route.get("id")
            if not isinstance(route_id, str) or route_id in route_ids:
                _issue(issues, "invalid_frontend_manifest", path, "route identifiers must be unique strings")
            route_ids.add(route_id)
            if route.get("kind") == "list":
                filters = route.get("allowed_filters", [])
                orders = route.get("allowed_orders", [])
                fields = set(route.get("fields", []))
                if route.get("record_id_field") != "name":
                    _issue(issues, "invalid_frontend_manifest", path, "list routes must use Frappe record_id_field 'name'")
                if not isinstance(filters, list) or not isinstance(orders, list):
                    _issue(issues, "invalid_frontend_manifest", path, "list routes require filter and order arrays")
                    continue
                for query in filters:
                    if not isinstance(query, Mapping) or query.get("name") not in fields:
                        _issue(issues, "invalid_frontend_manifest", path, "list filters must be declared list fields")
                for order in orders:
                    if not isinstance(order, str) or ":" not in order or order.rsplit(":", 1)[0] not in fields:
                        _issue(issues, "invalid_frontend_manifest", path, "list orders must be declared list fields")
        for item in navigation:
            if not isinstance(item, Mapping) or item.get("route_id") not in route_ids:
                _issue(issues, "invalid_frontend_manifest", path, "navigation must reference a declared route")


def _audit_doctype_json(path: str, value: Any, issues: list[ArtifactAuditIssue]) -> None:
    if not isinstance(value, Mapping):
        _issue(issues, "invalid_doctype_json", path, "DocType metadata must be a JSON object")
        return
    if value.get("doctype") != "DocType" or not isinstance(value.get("name"), str):
        _issue(issues, "invalid_doctype_json", path, "DocType metadata requires doctype='DocType' and a name")
    fields = value.get("fields")
    if not isinstance(fields, list) or not all(isinstance(field, Mapping) and isinstance(field.get("fieldname"), str) and isinstance(field.get("fieldtype"), str) for field in fields):
        _issue(issues, "invalid_doctype_fields", path, "DocType fields must be objects with fieldname and fieldtype")
    elif any(field.get("fieldtype") == "URL" for field in fields):
        _issue(issues, "unsupported_frappe_fieldtype", path, "Frappe URL semantics must render as Data with options='URL'")
    permissions = value.get("permissions")
    if not isinstance(permissions, list) or not all(isinstance(permission, Mapping) and isinstance(permission.get("role"), str) for permission in permissions):
        _issue(issues, "invalid_doctype_permissions", path, "DocType permissions must be objects with role")


def _audit_verification_plan(path: str, value: Any, issues: list[ArtifactAuditIssue]) -> None:
    try:
        load_verification_program(value)
    except VerificationProgramError as error:
        _issue(issues, "invalid_verification_plan", path, str(error))


def _audit_verification_summary(path: str, value: Any, issues: list[ArtifactAuditIssue]) -> None:
    if not isinstance(value, Mapping) or value.get("version") != 1 or value.get("execution") != "not_executed":
        _issue(issues, "invalid_verification_summary", path, "verification summary must be a V1 unexecuted summary")
        return
    if not _HASH.fullmatch(value.get("spec_hash", "")) or not _HASH.fullmatch(value.get("template_hash", "")):
        _issue(issues, "invalid_verification_summary", path, "verification summary must bind spec and template SHA-256 hashes")
    if isinstance(value, Mapping):
        if not isinstance(value.get("case_count"), int) or value["case_count"] < 0:
            _issue(issues, "invalid_verification_summary", path, "verification summary case_count must be a non-negative integer")
        cases_by_suite = value.get("cases_by_suite")
        if not isinstance(cases_by_suite, Mapping) or any(
            not isinstance(name, str) or not isinstance(count, int) or count < 0
            for name, count in cases_by_suite.items()
        ):
            _issue(issues, "invalid_verification_summary", path, "verification summary cases_by_suite must map suite names to counts")


def _audit_entity_verification_cases(path: str, relative_path: str, value: Any, issues: list[ArtifactAuditIssue]) -> None:
    if not re.fullmatch(r"harness/tests/[a-z][a-z0-9_]*\.json", relative_path):
        _issue(issues, "unsupported_artifact_path", path, "verification case artifact name must be a safe entity identifier")
        return
    if not isinstance(value, Mapping) or value.get("version") != 1 or not isinstance(value.get("entity"), str):
        _issue(issues, "invalid_verification_cases", path, "entity verification cases must be a V1 identified object")
        return
    if not _HASH.fullmatch(value.get("spec_hash", "")) or not isinstance(value.get("cases"), list):
        _issue(issues, "invalid_verification_cases", path, "entity verification cases require spec hash and case list")


def _audit_python(path: str, content: bytes, issues: list[ArtifactAuditIssue], *, app_slug: str | None) -> None:
    try:
        tree = ast.parse(content.decode("utf-8"), filename=path)
    except (UnicodeDecodeError, SyntaxError) as error:
        _issue(issues, "invalid_python", path, f"artifact is not valid Python: {error}")
        return
    relative_path = _app_relative(path, app_slug)
    if app_slug and relative_path == f"{app_slug}/hooks.py":
        _audit_hooks(tree, path, issues)
    elif app_slug and relative_path == f"{app_slug}/api.py":
        _audit_frontend_api(tree, path, issues)
    elif app_slug and _is_package_initializer(relative_path, app_slug):
        _audit_package_initializer(tree, path, issues, is_app_package=relative_path == f"{app_slug}/__init__.py")
    elif "/doctype/" in relative_path:
        _audit_controller(tree, path, issues)
    else:
        _issue(issues, "unsupported_python_artifact", path, "only hooks.py and generated DocType controllers are supported")


def _app_relative(path: str, app_slug: str | None) -> str:
    """Return the path inside a manifest-identified deployable app bundle."""

    prefix = f"{app_slug}/" if app_slug else ""
    return path[len(prefix):] if prefix and path.startswith(prefix) else path


def _audit_hooks(tree: ast.Module, path: str, issues: list[ArtifactAuditIssue]) -> None:
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            _issue(issues, "unsupported_hook_syntax", path, "hooks.py may contain only literal assignments")
            continue
        name = node.targets[0].id
        names.add(name)
        if name not in _SUPPORTED_HOOKS:
            _issue(issues, "unsupported_hook", path, f"hook {name!r} is outside the V1 allowlist")
        try:
            ast.literal_eval(node.value)
        except ValueError:
            _issue(issues, "nonliteral_hook", path, f"hook {name!r} must have a literal value")
    for required in ("app_name", "app_title", "required_apps", "fixtures"):
        if required not in names:
            _issue(issues, "missing_hook", path, f"required app metadata hook {required!r} is absent")


def _audit_frontend_api(tree: ast.Module, path: str, issues: list[ArtifactAuditIssue]) -> None:
    """Keep the generated manifest endpoint narrow and data-only."""
    names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    if "frontend_manifest" not in names:
        _issue(issues, "unsupported_api", path, "generated api.py must define frontend_manifest")
    allowed_imports = {("__future__", "annotations"), ("json", None), ("pathlib", "Path"), ("frappe", None)}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if (alias.name, alias.asname) not in allowed_imports:
                    _issue(issues, "unsupported_api", path, "frontend api imports are outside the fixed allowlist")
        elif isinstance(node, ast.ImportFrom):
            if (node.module, node.names[0].name if node.names else None) not in allowed_imports:
                _issue(issues, "unsupported_api", path, "frontend api imports are outside the fixed allowlist")


def _is_package_initializer(relative_path: str, app_slug: str) -> bool:
    """Recognize only the canonical Frappe app/DocType package markers."""

    return relative_path == f"{app_slug}/__init__.py" or re.fullmatch(
        rf"{re.escape(app_slug)}/[a-z][a-z0-9_]*/(?:__init__\.py|doctype/__init__\.py|doctype/[a-z][a-z0-9_]*/__init__\.py)",
        relative_path,
    ) is not None


def _audit_package_initializer(
    tree: ast.Module, path: str, issues: list[ArtifactAuditIssue], *, is_app_package: bool
) -> None:
    """Permit only Frappe's declarative app version in the root package."""

    if not tree.body:
        return
    if is_app_package and len(tree.body) == 1:
        statement = tree.body[0]
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id == "__version__"
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
            and statement.value.value == "0.0.1"
        ):
            return
    _issue(
        issues,
        "unsupported_package_initializer",
        path,
        "only the app-root __version__ = '0.0.1' declaration is permitted in V1 package initializers",
    )


def _audit_controller(tree: ast.Module, path: str, issues: list[ArtifactAuditIssue]) -> None:
    allowed_import = "frappe.model.document"
    class_nodes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    imports = [node for node in tree.body if isinstance(node, ast.ImportFrom)]
    if len(tree.body) != 2 or len(imports) != 1 or len(class_nodes) != 1:
        _issue(issues, "unsupported_controller", path, "controllers may contain only the standard Document import and one empty class")
        return
    imported = imports[0]
    if imported.module != allowed_import or [(alias.name, alias.asname) for alias in imported.names] != [("Document", None)]:
        _issue(issues, "unsupported_api", path, "controller imports must be exactly frappe.model.document.Document")
    controller = class_nodes[0]
    if [base.id for base in controller.bases if isinstance(base, ast.Name)] != ["Document"] or len(controller.bases) != 1:
        _issue(issues, "unsupported_controller", path, "controller must directly inherit Document")
    if len(controller.body) != 1 or not isinstance(controller.body[0], ast.Pass):
        _issue(issues, "unsupported_controller", path, "controller methods and executable code are unsupported in V1")


def _audit_required_structure(artifacts: Mapping[str, bytes], manifest: Mapping[str, Any] | None, issues: list[ArtifactAuditIssue]) -> None:
    if not isinstance(manifest, Mapping):
        return
    app_slug = manifest.get("app_slug")
    if not isinstance(app_slug, str) or not _IDENTIFIER.fullmatch(app_slug):
        _issue(issues, "invalid_app_slug", "<app>/harness/ownership-manifest.json", "manifest app_slug must be a valid identifier")
        return
    app_root = app_slug
    package_root = f"{app_root}/{app_slug}"
    harness_root = f"{app_root}/harness"
    module_slug = _module_slug(_json_object(artifacts.get(f"{harness_root}/spec.json")))
    if module_slug is None:
        _issue(issues, "invalid_module_path", f"{harness_root}/spec.json", "spec module must derive a safe Frappe module package name")
        return
    module_root = f"{package_root}/{module_slug}"
    required = {
        f"{package_root}/__init__.py",
        f"{module_root}/__init__.py",
        f"{module_root}/doctype/__init__.py",
        f"{package_root}/hooks.py",
        f"{package_root}/modules.txt",
        f"{app_root}/fixtures/roles.json",
        f"{harness_root}/spec.json",
        f"{harness_root}/ownership-manifest.json",
    }
    if _is_typed_project_spec(_json_object(artifacts.get(f"{harness_root}/spec.json"))):
        required.add(f"{harness_root}/frontend-manifest.json")
        required.add(f"{package_root}/api.py")
    for path in sorted(required - set(artifacts)):
        _issue(issues, "missing_required_artifact", path, "required compiler artifact is absent")
    verification_paths = {
        path for path in artifacts
        if path in {f"{harness_root}/verification-plan.json", f"{harness_root}/verification-summary.json"}
        or path.startswith(f"{harness_root}/tests/")
    }
    if verification_paths:
        _audit_verification_bundle(artifacts, verification_paths, issues, harness_root=harness_root)
    for path in artifacts:
        if path in required:
            continue
        if path == f"{harness_root}/frontend-manifest.json":
            continue
        if path in verification_paths:
            continue
        frontend_match = re.fullmatch(
            rf"{re.escape(package_root)}/(?:public/harness/assets/[A-Za-z0-9._-]+|www/{re.escape(app_slug)}\.html)",
            path,
        )
        if frontend_match:
            continue
        doctype_match = re.fullmatch(rf"{re.escape(module_root)}/doctype/([a-z][a-z0-9_]*)/(?:\1\.(?:json|py)|__init__\.py)", path)
        if not doctype_match:
            _issue(issues, "unsupported_artifact_path", path, "artifact is outside the constrained V1 app layout")
    doctype_bases = {
        path.rsplit("/", 1)[0]
        for path in artifacts
        if re.fullmatch(rf"{re.escape(module_root)}/doctype/[a-z][a-z0-9_]*/(?:[a-z][a-z0-9_]*\.(?:json|py)|__init__\.py)", path)
    }
    for base in sorted(doctype_bases):
        directory = base.rsplit("/", 1)[-1]
        for filename in (f"{directory}.json", f"{directory}.py", "__init__.py"):
            if f"{base}/{filename}" not in artifacts:
                _issue(issues, "incomplete_doctype_artifact", base, "each generated DocType requires JSON, controller, and package initializer files")


def _json_object(content: Any) -> Mapping[str, Any] | None:
    if not isinstance(content, bytes):
        return None
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _module_slug(spec_snapshot: Mapping[str, Any] | None) -> str | None:
    """Return the scrubbed module package name represented by a spec snapshot.

    The compiler still supports the pre-contract fixture shape, where the
    module is called ``module_name``; current ProjectSpec snapshots use
    ``module``.  Both names have the same Frappe package semantics.
    """

    if not isinstance(spec_snapshot, Mapping):
        return None
    value = spec_snapshot.get("module", spec_snapshot.get("module_name"))
    if not isinstance(value, str):
        return None
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug if _IDENTIFIER.fullmatch(slug) else None


def _is_typed_project_spec(value: Mapping[str, Any] | None) -> bool:
    """Recognize the modern ProjectSpec snapshot but retain legacy previews."""

    return (
        isinstance(value, Mapping)
        and all(isinstance(value.get(key), str) for key in ("name", "label", "module"))
        and isinstance(value.get("entities"), list)
        and isinstance(value.get("roles"), list)
        and isinstance(value.get("screens"), list)
    )


def _audit_verification_bundle(
    artifacts: Mapping[str, bytes], paths: set[str], issues: list[ArtifactAuditIssue], *, harness_root: str,
) -> None:
    """Check the optional, all-or-nothing typed verification artifact bundle.

    Legacy compiler callers legitimately do not produce this bundle.  Once one
    of its paths exists, though, partial output or hash disagreement must never
    be accepted as durable evidence.
    """

    required = {f"{harness_root}/verification-plan.json", f"{harness_root}/verification-summary.json"}
    for path in sorted(required - paths):
        _issue(issues, "incomplete_verification_bundle", path, "verification artifacts require both plan and summary")
    entity_paths = sorted(path for path in paths if path.startswith(f"{harness_root}/tests/"))
    if not entity_paths:
        _issue(issues, "incomplete_verification_bundle", f"{harness_root}/tests", "verification artifacts require at least one entity case file")
    try:
        plan = json.loads(artifacts[f"{harness_root}/verification-plan.json"].decode("utf-8"))
        summary = json.loads(artifacts[f"{harness_root}/verification-summary.json"].decode("utf-8"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError):
        return  # The regular JSON audit reports malformed bytes.
    if not isinstance(plan, Mapping) or not isinstance(summary, Mapping):
        return
    try:
        program = load_verification_program(plan)
    except VerificationProgramError:
        return  # The regular plan audit reports the precise malformed plan.
    if plan.get("spec_hash") != summary.get("spec_hash") or plan.get("template_hash") != summary.get("template_hash"):
        _issue(issues, "verification_hash_mismatch", f"{harness_root}/verification-summary.json", "plan and summary must bind the same spec and template hashes")
    expected_suites: dict[str, int] = {}
    for case in program.cases:
        expected_suites[case.suite] = expected_suites.get(case.suite, 0) + 1
    if summary.get("case_count") != len(program.cases) or summary.get("cases_by_suite") != dict(sorted(expected_suites.items())):
        _issue(issues, "verification_summary_mismatch", f"{harness_root}/verification-summary.json", "verification summary must exactly describe the hash-bound plan")
    planned_entities = {case.get("entity") for case in plan.get("cases", ()) if isinstance(case, Mapping) and isinstance(case.get("entity"), str)}
    emitted_entities: set[str] = set()
    for path in entity_paths:
        try:
            entity_cases = json.loads(artifacts[path].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(entity_cases, Mapping):
            continue
        entity = entity_cases.get("entity")
        if isinstance(entity, str):
            emitted_entities.add(entity)
        if entity not in planned_entities or entity_cases.get("spec_hash") != plan.get("spec_hash"):
            _issue(issues, "verification_entity_mismatch", path, "entity case file must bind to an entity and spec in the verification plan")
    if emitted_entities != planned_entities:
        _issue(issues, "incomplete_verification_bundle", f"{harness_root}/tests", "entity case files must cover exactly the entities in the verification plan")


def _audit_ownership(artifacts: Mapping[str, bytes], manifest: Mapping[str, Any] | None, expected_spec_hash: str | None, issues: list[ArtifactAuditIssue]) -> None:
    if not isinstance(manifest, Mapping):
        return
    manifest_path = _manifest_path(_manifest_app_slug(manifest) or "<app>")
    if manifest.get("manifest_version") != 1:
        _issue(issues, "unsupported_manifest_version", manifest_path, "only manifest version 1 is supported")
    spec_hash = manifest.get("spec_hash")
    if not isinstance(spec_hash, str) or not _HASH.fullmatch(spec_hash):
        _issue(issues, "invalid_spec_hash", manifest_path, "manifest spec_hash must be a SHA-256 hex digest")
    elif expected_spec_hash is not None and spec_hash != expected_spec_hash:
        _issue(issues, "spec_hash_mismatch", manifest_path, "manifest does not bind to the expected confirmed spec hash")
    entries = manifest.get("owned_files")
    if not isinstance(entries, list):
        _issue(issues, "invalid_ownership", manifest_path, "owned_files must be a list")
        return
    seen: set[str] = set()
    expected = set(artifacts) - {manifest_path}
    listed: set[str] = set()
    previous = ""
    for index, entry in enumerate(entries):
        entry_path = f"{manifest_path}.owned_files[{index}]"
        if not isinstance(entry, Mapping) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("sha256"), str):
            _issue(issues, "invalid_ownership_entry", entry_path, "ownership entries require path and sha256 strings")
            continue
        path, digest = entry["path"], entry["sha256"]
        listed.add(path)
        if path in seen:
            _issue(issues, "duplicate_owned_path", entry_path, f"owned path {path!r} appears more than once")
        seen.add(path)
        if path < previous:
            _issue(issues, "unsorted_ownership", entry_path, "owned_files must be sorted by path")
        previous = path
        _audit_path(path, issues)
        if not _HASH.fullmatch(digest):
            _issue(issues, "invalid_artifact_hash", entry_path, "ownership digest must be a SHA-256 hex digest")
        elif path in artifacts and isinstance(artifacts[path], bytes) and sha256(artifacts[path]).hexdigest() != digest:
            _issue(issues, "ownership_hash_mismatch", path, "artifact bytes do not match the recorded ownership digest")
    for path in sorted(expected - listed):
        _issue(issues, "unowned_artifact", path, "compiler artifact is absent from ownership manifest")
    for path in sorted(listed - expected):
        _issue(issues, "missing_owned_artifact", path, "ownership manifest references an absent or manifest-only artifact")


def _issue(issues: list[ArtifactAuditIssue], code: str, path: str, message: str) -> None:
    issues.append(ArtifactAuditIssue(code, path, message))
