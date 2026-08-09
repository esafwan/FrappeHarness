"""Typed, read-only Frappe metadata inspection for migration reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .frappe_rest_provider import FrappeRestProvider
from .provider_plan import MetadataPlan
from .reconciliation_probe import MigrationInspectionFacts
from .run_store import LockTarget


class FrappeMigrationInspectionError(RuntimeError):
    """The fixed metadata inspection could not establish an exact binding."""


@dataclass(frozen=True)
class FrappeMetadataFieldExpectation:
    fieldname: str
    fieldtype: str
    options: str | None = None


@dataclass(frozen=True)
class FrappeMigrationInspectionConfig:
    """Exact run-bound identity and the fixed DocType metadata target."""

    target: LockTarget
    spec_hash: str
    plan_hash: str
    doctype: str = "Task"
    expected_fields: tuple[FrappeMetadataFieldExpectation, ...] = ()

    @classmethod
    def from_plan(cls, target: LockTarget, plan: MetadataPlan, *, doctype: str) -> "FrappeMigrationInspectionConfig":
        operation = next((item for item in plan.operations if item.doctype == doctype), None)
        if operation is None:
            raise FrappeMigrationInspectionError("inspection DocType is absent from the bound provider plan")
        doctype_by_entity = {item.entity: item.doctype for item in plan.operations}
        expected = tuple(
            FrappeMetadataFieldExpectation(
                field.name,
                field.frappe_field_type,
                field.frappe_options
                or (
                    "\n".join(field.select_options)
                    if field.frappe_field_type == "Select"
                    else doctype_by_entity.get(field.link_target or "")
                ),
            )
            for field in operation.fields
        )
        return cls(target, plan.spec_hash, plan.plan_hash, doctype, expected)


class FrappeMigrationInspectionProbe:
    """Inspect a known DocType through the closed Frappe REST provider.

    This proves only that the target is reachable and its current metadata is
    inspectable.  It intentionally returns ``inspected`` and never infers that
    a migration was applied.
    """

    def __init__(self, provider: FrappeRestProvider, config: FrappeMigrationInspectionConfig) -> None:
        self.provider = provider
        self.config = config

    def inspect(self, target: LockTarget) -> MigrationInspectionFacts:
        if target != self.config.target:
            raise FrappeMigrationInspectionError("inspection target does not match the bound target")
        result = self.provider.inspect_meta(self.config.doctype)
        if not result.ok or not isinstance(result.payload, Mapping):
            raise FrappeMigrationInspectionError("Frappe metadata inspection was unavailable")
        data = result.payload.get("data")
        if not isinstance(data, Mapping):
            raise FrappeMigrationInspectionError("Frappe metadata response has no typed data object")
        fields = data.get("fields")
        if not isinstance(fields, list) or not all(isinstance(field, Mapping) for field in fields):
            raise FrappeMigrationInspectionError("Frappe metadata response has no typed fields list")
        expectations = self.config.expected_fields or (FrappeMetadataFieldExpectation("reference_url", "Data", "URL"),)
        by_name = {field.get("fieldname"): field for field in fields}
        for expected in expectations:
            observed = by_name.get(expected.fieldname)
            if observed is None or observed.get("fieldtype") != expected.fieldtype or observed.get("options") != expected.options:
                raise FrappeMigrationInspectionError(f"expected {expected.fieldname} metadata field is absent or mismatched")
        reference = f"frappe-rest:{result.receipt.operation}:{result.receipt.status}:{result.receipt.response_sha256}"
        return MigrationInspectionFacts(reference, target, self.config.spec_hash, self.config.plan_hash, "inspected")
