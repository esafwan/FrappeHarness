"""Deterministic semantic plans for Frappe-native metadata providers.

Plans deliberately contain no shell text, raw REST path, SQL, Commander field
DSL, or unvalidated Frappe metadata dictionary. A trusted provider adapter may
translate a plan into its own pinned API only after core approval.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import json
from typing import Any

from .contracts import FieldSpec, FieldType, ProjectSpec, spec_hash, validate_project_spec
from .frappe_field_adapter import project_field_type


class ProviderPlanError(ValueError):
    """Raised when a semantic specification cannot make a safe provider plan."""


@dataclass(frozen=True)
class ProviderField:
    name: str
    label: str
    field_type: str
    required: bool
    unique: bool
    default: Any | None
    select_options: tuple[str, ...] = ()
    link_target: str | None = None
    frappe_field_type: str = ""
    frappe_options: str | None = None


@dataclass(frozen=True)
class ProviderPermission:
    role: str
    read: bool
    create: bool
    write: bool
    delete: bool


@dataclass(frozen=True)
class ProviderOperation:
    id: str
    kind: str
    entity: str
    doctype: str
    module: str
    fields: tuple[ProviderField, ...]
    permissions: tuple[ProviderPermission, ...]
    depends_on: tuple[str, ...]
    precondition: str
    postcondition: str


@dataclass(frozen=True)
class MetadataPlan:
    version: int
    provider: str
    target_frappe_major: int
    spec_hash: str
    operations: tuple[ProviderOperation, ...]
    plan_hash: str


def build_metadata_plan(spec: ProjectSpec, *, provider: str = "frappe-native") -> MetadataPlan:
    """Build a validated, target-independent plan in safe Link dependency order."""
    report = validate_project_spec(spec)
    report.require_valid()
    if provider not in {"frappe-native", "commander-spike"}:
        raise ProviderPlanError(f"unknown provider {provider!r}")

    pending = {entity.name: entity for entity in spec.entities}
    operations: list[ProviderOperation] = []
    completed: set[str] = set()
    while pending:
        ready = [entity for entity in pending.values() if _link_targets(entity) <= completed]
        if not ready:
            # Required Link cycles are already rejected by contract validation;
            # this catches any future optional/unsupported provider dependency.
            raise ProviderPlanError("could not establish safe Link creation order")
        for entity in sorted(ready, key=lambda item: item.name):
            dependencies = tuple(f"create:{target}" for target in sorted(_link_targets(entity)))
            operations.append(
                ProviderOperation(
                    id=f"create:{entity.name}",
                    kind="create_doctype",
                    entity=entity.name,
                    doctype=entity.label,
                    module=spec.module,
                    fields=tuple(_provider_field(field) for field in entity.fields),
                    permissions=tuple(
                        ProviderPermission(permission.role, permission.read, permission.create, permission.write, permission.delete)
                        for permission in sorted(entity.permissions, key=lambda item: item.role)
                    ),
                    depends_on=dependencies,
                    precondition=f"doctype_absent:{entity.label}",
                    postcondition=f"metadata_matches:{entity.name}",
                )
            )
            completed.add(entity.name)
            del pending[entity.name]
    hash_input = {
        "version": 1,
        "provider": provider,
        "target_frappe_major": 16,
        "spec_hash": spec_hash(spec),
        "operations": [asdict(operation) for operation in operations],
    }
    plan_hash = hashlib.sha256(_canonical_json(hash_input).encode("utf-8")).hexdigest()
    return MetadataPlan(1, provider, 16, spec_hash(spec), tuple(operations), plan_hash)


def plan_json(plan: MetadataPlan) -> str:
    """Return canonical JSON for an auditable plan artifact."""
    return _canonical_json(asdict(plan)) + "\n"


def plan_hash_document(plan: MetadataPlan) -> dict[str, Any]:
    """Return the canonical hash-input object used as a semantic plan ref.

    The digest of its canonical JSON bytes is exactly ``plan.plan_hash``;
    operator bundles can therefore bind file and run-intent hashes together.
    """
    return {
        "version": plan.version,
        "provider": plan.provider,
        "target_frappe_major": plan.target_frappe_major,
        "spec_hash": plan.spec_hash,
        "operations": [asdict(operation) for operation in plan.operations],
    }


def _provider_field(field: FieldSpec) -> ProviderField:
    link_target = field.options[0] if field.field_type is FieldType.LINK else None
    select_options = field.options if field.field_type is FieldType.SELECT else ()
    projection = project_field_type(field.field_type)
    return ProviderField(field.name, field.label, field.field_type.value, field.required, field.unique, field.default, select_options, link_target, projection.fieldtype, projection.options)


def _link_targets(entity: Any) -> set[str]:
    return {
        field.options[0]
        for field in entity.fields
        if field.field_type is FieldType.LINK and field.options
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
