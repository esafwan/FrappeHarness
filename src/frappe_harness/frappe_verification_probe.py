"""Trusted adapter from declarative verification cases to closed Frappe REST.

The probe has no network capability of its own.  It only delegates to injected
``FrappeRestProvider``-shaped clients, records symbolic names in memory, and
returns the outcome string consumed by :mod:`verification_program`.  Error
messages and document payloads never enter its observations or reports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol

from .frappe_rest_provider import DocumentQuery, EqualityFilter, FrappeRestResult, RestErrorKind
from .verification_program import (
    BackendVerificationRequest,
    PermissionVerificationRequest,
    RestVerificationRequest,
    VerificationObservation,
    VerificationRequest,
)


UNMAPPABLE = "unmappable"
PROVIDER_FAILURE = "provider_failure"


class FrappeVerificationProvider(Protocol):
    """The closed Frappe REST provider surface used by this probe."""

    def list_documents(self, doctype: str, query: DocumentQuery = DocumentQuery()) -> FrappeRestResult: ...

    def get_document(self, doctype: str, name: str) -> FrappeRestResult: ...

    def create_document(self, doctype: str, payload: Mapping[str, Any]) -> FrappeRestResult: ...

    def update_document(self, doctype: str, name: str, payload: Mapping[str, Any]) -> FrappeRestResult: ...

    def delete_document(self, doctype: str, name: str) -> FrappeRestResult: ...


@dataclass(frozen=True)
class VerificationFixtures:
    """Pre-created, disposable records and valid payloads for one run.

    A caller must allocate a delete target for each exact verification case
    ID. The probe intentionally does not fall back to a normal fixture record
    for a delete operation, preventing cross-case reuse or deletion of shared
    evidence.
    """

    valid_payloads: Mapping[str, Mapping[str, Any]]
    record_names: Mapping[str, str]
    update_payloads: Mapping[str, Mapping[str, Any]]
    delete_names: Mapping[str, str] = field(default_factory=dict)

    def payload_for(self, entity: str) -> dict[str, Any] | None:
        payload = self.valid_payloads.get(entity)
        return dict(payload) if isinstance(payload, Mapping) else None

    def update_for(self, entity: str) -> dict[str, Any] | None:
        payload = self.update_payloads.get(entity)
        return dict(payload) if isinstance(payload, Mapping) else None

    def record_for(self, entity: str) -> str | None:
        value = self.record_names.get(entity)
        return value if isinstance(value, str) and value else None

    def delete_for(self, case_id: str) -> str | None:
        value = self.delete_names.get(case_id)
        return value if isinstance(value, str) and value else None


def derive_verification_fixtures(
    records: Mapping[str, Mapping[str, Any]],
    case_ids: Iterable[str],
) -> VerificationFixtures:
    """Build a run-local fixture skeleton from already observed records.

    This is deliberately a pure transformation: it performs no REST calls and
    never invents a record name. ``records`` is keyed by the compiler entity
    names (for example ``project`` and ``task``), and each value must contain a
    non-empty ``name``. Case IDs are used only to allocate delete targets, so a
    delete case can never fall back to a shared record. Payloads are copied and
    server metadata is omitted from the create template.
    """
    if not isinstance(records, Mapping) or not records:
        raise ValueError("records must be a non-empty mapping")
    names: dict[str, str] = {}
    valid: dict[str, dict[str, Any]] = {}
    updates: dict[str, dict[str, Any]] = {}
    for entity, record in records.items():
        if not isinstance(entity, str) or not entity or not isinstance(record, Mapping):
            raise ValueError("records must map entity names to objects")
        name = record.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"record {entity!r} is missing a name")
        names[entity] = name
        payload = {
            str(key): value
            for key, value in record.items()
            if key not in {"name", "doctype", "owner", "creation", "modified", "modified_by", "idx"}
            and not str(key).startswith("_")
        }
        # Frappe serializes Check fields as 0/1 while the typed verification
        # template uses booleans. Normalize only the canonical Check field
        # names; do not coerce arbitrary numeric business values.
        if "active" in payload and isinstance(payload["active"], int) and not isinstance(payload["active"], bool):
            payload["active"] = bool(payload["active"])
        if not payload:
            raise ValueError(f"record {entity!r} has no usable fields")
        valid[entity] = payload
        title_field = "title" if "title" in payload else "subject" if "subject" in payload else None
        updates[entity] = {title_field: f"Harness updated {entity}"} if title_field else dict(payload)

    deletes: dict[str, str] = {}
    for case_id in case_ids:
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("case IDs must be non-empty strings")
        parts = case_id.split(":")
        if len(parts) >= 4 and parts[0] in {"permissions", "rest"} and parts[2] == "delete":
            entity = parts[1]
            if entity in names:
                deletes[case_id] = names[entity]
    return VerificationFixtures(valid_payloads=valid, record_names=names, update_payloads=updates, delete_names=deletes)


@dataclass(frozen=True)
class CreatedRecord:
    """A process-local generated name which a trusted runner must clean up."""

    actor: str
    entity: str
    name: str


class FrappeVerificationProbe:
    """Map all current V1 semantic cases to closed document operations."""

    def __init__(self, actors: Mapping[str, FrappeVerificationProvider], fixtures: VerificationFixtures) -> None:
        self._actors = dict(actors)
        self._fixtures = fixtures
        self._created: dict[tuple[str, str], str] = {}
        self._cleanup_records: list[CreatedRecord] = []

    @property
    def cleanup_records(self) -> tuple[CreatedRecord, ...]:
        """Names of every successful create, kept outside durable reports."""

        return tuple(self._cleanup_records)

    def probe(self, request: VerificationRequest) -> VerificationObservation:
        provider = self._actors.get(request.case.actor)
        if provider is None:
            return VerificationObservation(UNMAPPABLE)
        if isinstance(request, BackendVerificationRequest):
            return self._backend(provider, request)
        if isinstance(request, PermissionVerificationRequest):
            return self._permission(provider, request)
        if isinstance(request, RestVerificationRequest):
            return self._rest(provider, request)
        return VerificationObservation(UNMAPPABLE)

    def _backend(self, provider: FrappeVerificationProvider, request: BackendVerificationRequest) -> VerificationObservation:
        case = request.case
        entity, operation = case.entity, case.operation
        if operation == "read_created":
            return _outcome(provider.get_document(_doctype(entity), self._created_name("administrator", entity)), "success") if self._created_name("administrator", entity) else _unmappable()
        if operation == "update_valid":
            name = self._created_name("administrator", entity)
            template_changes = _mapping(case.inputs.get("changes"))
            changes = self._fixtures.update_for(entity)
            if not name or template_changes is None or changes is None or not _compatible_mapping(template_changes, changes):
                return _unmappable()
            return _outcome(provider.update_document(_doctype(entity), name, self._resolve_payload(changes)), "success")
        payload = self._fixtures.payload_for(entity)
        if payload is None:
            return _unmappable()
        if operation == "create_valid":
            template_payload = _mapping(case.inputs.get("payload"))
            if template_payload is None or not _compatible_mapping(template_payload, payload):
                return _unmappable()
            return self._created_outcome(provider.create_document(_doctype(entity), self._resolve_payload(payload)), "administrator", entity, "success", primary=True)
        if operation == "create_missing_required":
            field = _text(case.inputs.get("omitted_field"))
            if field is None:
                return _unmappable()
            payload.pop(field, None)
            return self._created_outcome(provider.create_document(_doctype(entity), self._resolve_payload(payload)), "administrator", entity, "validation_error")
        if operation == "create_duplicate_unique":
            field, value = _text(case.inputs.get("field")), case.inputs.get("value")
            if field is None:
                return _unmappable()
            payload[field] = value
            return self._created_outcome(provider.create_document(_doctype(entity), self._resolve_payload(payload)), "administrator", entity, "validation_error")
        if operation in {"supported_field_type", "select_allowed", "select_rejected", "link_valid", "link_missing"}:
            field = _text(case.inputs.get("field"))
            if field is None:
                return _unmappable()
            value = case.inputs.get("value", case.inputs.get("fixture"))
            if operation == "link_missing":
                value = case.inputs.get("fixture")
            payload[field] = self._resolve_value(value)
            expected = "validation_error" if operation in {"select_rejected", "link_missing"} else "success"
            return self._created_outcome(provider.create_document(_doctype(entity), self._resolve_payload(payload)), "administrator", entity, expected)
        return _unmappable()

    def _permission(self, provider: FrappeVerificationProvider, request: PermissionVerificationRequest) -> VerificationObservation:
        case = request.case
        result = self._crud(provider, case.actor, case.entity, case.operation, case.id)
        if result is None:
            return _unmappable()
        return self._created_outcome(result, case.actor, case.entity, "allowed", permission_mode=True) if case.operation == "create" else _outcome(result, "allowed", permission_mode=True)

    def _rest(self, provider: FrappeVerificationProvider, request: RestVerificationRequest) -> VerificationObservation:
        case = request.case
        entity, operation = case.entity, case.operation
        if operation == "list":
            return _outcome(provider.list_documents(_doctype(entity)), "success")
        if operation == "list_filtered":
            field, value = _text(case.inputs.get("filter_field")), case.inputs.get("filter_value")
            if field is None:
                return _unmappable()
            return _outcome(provider.list_documents(_doctype(entity), DocumentQuery(filters=(EqualityFilter(field, value),))), "success")
        if operation == "list_ordered":
            field, direction = _text(case.inputs.get("order_by")), _text(case.inputs.get("order_direction"))
            if field is None or direction not in {"asc", "desc"}:
                return _unmappable()
            return _outcome(provider.list_documents(_doctype(entity), DocumentQuery(order_by=field, direction=direction)), "success")
        if operation == "list_paginated":
            start, length = case.inputs.get("page_start"), case.inputs.get("page_length")
            if isinstance(start, bool) or not isinstance(start, int) or isinstance(length, bool) or not isinstance(length, int):
                return _unmappable()
            return _outcome(provider.list_documents(_doctype(entity), DocumentQuery(start=start, limit=length)), "success")
        if operation in {"invalid_payload", "normalized_validation_error"}:
            field = _text(case.inputs.get("omitted_required_field"))
            payload = self._fixtures.payload_for(entity)
            if field is None or payload is None or field not in payload:
                return _unmappable()
            payload.pop(field)
            outcome = self._created_outcome(provider.create_document(_doctype(entity), self._resolve_payload(payload)), "administrator", entity, "validation_error")
            if operation == "normalized_validation_error" and outcome.outcome == "validation_error":
                return VerificationObservation("normalized_error")
            return outcome
        result = self._crud(provider, case.actor, entity, operation, case.id)
        if result is None:
            return _unmappable()
        return self._created_outcome(result, case.actor, entity, "success") if operation == "create" else _outcome(result, "success")

    def _crud(
        self, provider: FrappeVerificationProvider, actor: str, entity: str, operation: str, case_id: str | None = None,
    ) -> FrappeRestResult | None:
        doctype = _doctype(entity)
        if operation == "create":
            payload = self._fixtures.payload_for(entity)
            return provider.create_document(doctype, self._resolve_payload(payload)) if payload is not None else None
        if operation == "read":
            name = self._fixtures.record_for(entity)
            return provider.get_document(doctype, name) if name else None
        if operation in {"write", "update"}:
            name, payload = self._fixtures.record_for(entity), self._fixtures.update_for(entity)
            return provider.update_document(doctype, name, self._resolve_payload(payload)) if name and payload else None
        if operation == "delete":
            name = self._fixtures.delete_for(case_id) if case_id else None
            return provider.delete_document(doctype, name) if name else None
        return None

    def _resolve_payload(self, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        return {key: self._resolve_value(value) for key, value in (payload or {}).items()}

    def _resolve_value(self, value: Any) -> Any:
        if isinstance(value, Mapping) and set(value) == {"fixture_entity", "fixture_name"}:
            fixture = value.get("fixture_entity")
            return self._fixtures.record_for(fixture) if isinstance(fixture, str) else value
        return value

    def _created_name(self, actor: str, entity: str) -> str | None:
        return self._created.get((actor, entity))

    def _created_outcome(
        self,
        result: FrappeRestResult,
        actor: str,
        entity: str,
        expected: str,
        *,
        primary: bool = False,
        permission_mode: bool = False,
    ) -> VerificationObservation:
        outcome = _outcome(result, expected, permission_mode=permission_mode)
        if not result.ok:
            return outcome
        name = _name_from_payload(result.payload)
        if name is None:
            return _provider_failure(result)
        self._cleanup_records.append(CreatedRecord(actor, entity, name))
        if primary:
            self._created[(actor, entity)] = name
        return outcome


def _outcome(result: FrappeRestResult, success: str, *, permission_mode: bool = False) -> VerificationObservation:
    if result.ok:
        return VerificationObservation(success)
    if result.error_kind is RestErrorKind.VALIDATION:
        return VerificationObservation("validation_error")
    if result.error_kind is RestErrorKind.PERMISSION:
        return VerificationObservation("denied" if permission_mode else "permission_denied")
    if result.error_kind is RestErrorKind.AUTHENTICATION:
        return VerificationObservation("authentication_required")
    return _provider_failure(result)


def _provider_failure(result: FrappeRestResult) -> VerificationObservation:
    # Deliberately do not propagate payloads, server error text, URLs, or
    # document names to the verification-program report.
    del result
    return VerificationObservation(PROVIDER_FAILURE)


def _unmappable() -> VerificationObservation:
    return VerificationObservation(UNMAPPABLE)


def _doctype(entity: str) -> str:
    return " ".join(part.capitalize() for part in entity.split("_"))


def _mapping(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _name_from_payload(value: Any) -> str | None:
    if isinstance(value, Mapping):
        name = value.get("name")
        if isinstance(name, str) and name:
            return name
        data = value.get("data")
        if isinstance(data, Mapping):
            name = data.get("name")
            return name if isinstance(name, str) and name else None
    return None


def _compatible_mapping(template: Mapping[str, Any], fixture: Mapping[str, Any]) -> bool:
    """Ensure a run-specific fixture retains the template's field semantics."""

    return all(key in fixture and _compatible_value(value, fixture[key]) for key, value in template.items())


def _compatible_value(template: Any, fixture: Any) -> bool:
    if isinstance(template, Mapping) and set(template) == {"fixture_entity", "fixture_name"}:
        return isinstance(fixture, (str, Mapping))
    if isinstance(template, bool):
        return isinstance(fixture, bool)
    if isinstance(template, int) and not isinstance(template, bool):
        return isinstance(fixture, int) and not isinstance(fixture, bool)
    if isinstance(template, float):
        return isinstance(fixture, float)
    if isinstance(template, str):
        return isinstance(fixture, str)
    if template is None:
        return fixture is None
    return type(fixture) is type(template)
