"""Pure comparison of a semantic metadata plan with observed Frappe metadata.

The observer may obtain JSON with ``frappectl doctype show --json``.  This
module never invokes it: keeping retrieval outside the verifier prevents a
metadata check from becoming a general network or command capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .provider_plan import MetadataPlan, ProviderField, ProviderOperation


@dataclass(frozen=True)
class MetadataMismatch:
    code: str
    path: str
    expected: Any
    observed: Any
    message: str


@dataclass(frozen=True)
class MetadataVerificationReport:
    mismatches: tuple[MetadataMismatch, ...] = ()

    @property
    def matches(self) -> bool:
        return not self.mismatches


def verify_metadata_plan(
    plan: MetadataPlan, observed_doctypes: Mapping[str, Mapping[str, Any]],
) -> MetadataVerificationReport:
    """Compare expected semantic properties without requiring byte-for-byte JSON.

    ``observed_doctypes`` may be keyed by either semantic entity id or Frappe
    DocType name. Each payload can be the DocType JSON itself or a common REST
    envelope with its metadata under ``data``. Framework-owned extra fields and
    permission rows are ignored; every planned value must still agree exactly.
    """

    mismatches: list[MetadataMismatch] = []
    entity_to_doctype = {operation.entity: operation.doctype for operation in plan.operations}
    for operation in plan.operations:
        observed = _lookup_observed(operation, observed_doctypes)
        root = f"doctypes.{operation.doctype}"
        if observed is None:
            mismatches.append(_mismatch("missing_doctype", root, operation.doctype, None, "planned DocType was not observed"))
            continue
        _compare_fields(operation, observed, entity_to_doctype, mismatches)
        _compare_permissions(operation, observed, mismatches)
    return MetadataVerificationReport(tuple(mismatches))


def _lookup_observed(operation: ProviderOperation, documents: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any] | None:
    candidate = documents.get(operation.entity, documents.get(operation.doctype))
    if not isinstance(candidate, Mapping):
        return None
    data = candidate.get("data", candidate)
    return data if isinstance(data, Mapping) else None


def _compare_fields(
    operation: ProviderOperation,
    observed: Mapping[str, Any],
    entity_to_doctype: Mapping[str, str],
    mismatches: list[MetadataMismatch],
) -> None:
    raw_fields = observed.get("fields", ())
    field_map = {
        item.get("fieldname"): item
        for item in raw_fields
        if isinstance(item, Mapping) and isinstance(item.get("fieldname"), str)
    }
    for expected in operation.fields:
        path = f"doctypes.{operation.doctype}.fields.{expected.name}"
        actual = field_map.get(expected.name)
        if actual is None:
            mismatches.append(_mismatch("missing_field", path, expected.name, None, "planned field was not observed"))
            continue
        _equal("field_type", path + ".fieldtype", expected.frappe_field_type or expected.field_type, actual.get("fieldtype"), mismatches)
        _equal("required", path + ".reqd", expected.required, _as_bool(actual.get("reqd")), mismatches)
        _equal("unique", path + ".unique", expected.unique, _as_bool(actual.get("unique")), mismatches)
        # Frappe serializes Check defaults as string database values in raw
        # metadata (for example "1"), while the confirmed contract is typed.
        observed_default = _as_bool(actual.get("default")) if expected.field_type == "Check" else actual.get("default")
        _equal("default", path + ".default", expected.default, observed_default, mismatches)
        if expected.link_target is not None:
            target = entity_to_doctype.get(expected.link_target, expected.link_target)
            _equal("link_target", path + ".options", target, actual.get("options"), mismatches)
        if expected.select_options:
            _equal("select_options", path + ".options", expected.select_options, _select_options(actual.get("options")), mismatches)
        if expected.frappe_options is not None:
            _equal("frappe_options", path + ".options", expected.frappe_options, actual.get("options"), mismatches)


def _compare_permissions(
    operation: ProviderOperation,
    observed: Mapping[str, Any],
    mismatches: list[MetadataMismatch],
) -> None:
    raw_permissions = observed.get("permissions", ())
    permission_map = {
        row.get("role"): row
        for row in raw_permissions
        if isinstance(row, Mapping) and isinstance(row.get("role"), str) and _permlevel_zero(row)
    }
    for expected in operation.permissions:
        path = f"doctypes.{operation.doctype}.permissions.{expected.role}"
        actual = permission_map.get(expected.role)
        if actual is None:
            mismatches.append(_mismatch("missing_permission", path, expected.role, None, "planned role permission was not observed"))
            continue
        for name in ("read", "create", "write", "delete"):
            _equal("permission", path + f".{name}", getattr(expected, name), _as_bool(actual.get(name)), mismatches)


def _permlevel_zero(row: Mapping[str, Any]) -> bool:
    value = row.get("permlevel", 0)
    return value in (0, "0", None)


def _select_options(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(option.strip() for option in value.replace("\r\n", "\n").split("\n") if option.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(option) for option in value)
    return ()


def _as_bool(value: Any) -> bool:
    return value in (True, 1, "1", "true", "True")


def _equal(code: str, path: str, expected: Any, observed: Any, mismatches: list[MetadataMismatch]) -> None:
    if expected != observed:
        mismatches.append(_mismatch(code, path, expected, observed, "observed metadata differs from the approved plan"))


def _mismatch(code: str, path: str, expected: Any, observed: Any, message: str) -> MetadataMismatch:
    return MetadataMismatch(code, path, expected, observed, message)
