"""Pure typed execution semantics for generated verification templates.

The compiler emits declarative JSON only.  This module makes that declaration
executable *against an injected probe* without acquiring any capability to
open a network connection, run a process, access a Bench, or create a Frappe
document.  A later, separately approved provider may implement the probe
protocol; unit tests use a scripted in-memory probe.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re
from typing import Any, Mapping, Protocol

from .compiler import canonical_json


class VerificationProgramError(ValueError):
    """A generated verification plan is malformed or not hash-bound."""


_HASH_LENGTH = 64
_SUITE_OPERATIONS = {
    "backend": frozenset({
        "create_valid", "read_created", "update_valid", "create_missing_required",
        "create_duplicate_unique", "supported_field_type", "select_allowed",
        "select_rejected", "link_valid", "link_missing",
    }),
    "permissions": frozenset({"read", "create", "write", "delete"}),
    "rest": frozenset({
        "list", "read", "create", "update", "delete", "list_filtered",
        "list_ordered", "list_paginated", "invalid_payload", "normalized_validation_error",
    }),
}
_EXPECTED = {
    "backend": frozenset({"success", "validation_error"}),
    "permissions": frozenset({"allowed", "denied"}),
    "rest": frozenset({"success", "permission_denied", "authentication_required", "validation_error", "normalized_error"}),
}
_ACTOR = "actor"
_FIELD = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


@dataclass(frozen=True)
class ProgramCase:
    """One validated declarative case, preserving symbolic template values."""

    id: str
    suite: str
    entity: str
    operation: str
    actor: str
    expected: str
    inputs: Mapping[str, Any]
    depends_on: tuple[str, ...]


@dataclass(frozen=True)
class VerificationProgram:
    """A verified, topologically executable, artifact-bound case program."""

    version: int
    target_frappe_major: int
    spec_hash: str
    template_hash: str
    cases: tuple[ProgramCase, ...]


@dataclass(frozen=True)
class BackendVerificationRequest:
    case: ProgramCase


@dataclass(frozen=True)
class PermissionVerificationRequest:
    case: ProgramCase


@dataclass(frozen=True)
class RestVerificationRequest:
    case: ProgramCase


VerificationRequest = BackendVerificationRequest | PermissionVerificationRequest | RestVerificationRequest


@dataclass(frozen=True)
class VerificationObservation:
    """A normalized result supplied by a trusted, external verification probe."""

    outcome: str


class VerificationProbe(Protocol):
    """Capability boundary for executing one already typed verification case."""

    def probe(self, request: VerificationRequest) -> VerificationObservation: ...


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    expected: str
    observed: str | None
    status: str
    code: str


@dataclass(frozen=True)
class VerificationRunReport:
    program_hash: str
    results: tuple[CaseResult, ...]

    @property
    def passed(self) -> bool:
        return all(result.status == "passed" for result in self.results)


def load_verification_program(value: Mapping[str, Any]) -> VerificationProgram:
    """Parse and authenticate a compiler-generated plan without performing I/O."""

    if not isinstance(value, Mapping):
        raise VerificationProgramError("verification plan must be an object")
    _equal(value.get("version"), 1, "verification plan version must be 1")
    _equal(value.get("target_frappe_major"), 16, "verification plan must target Frappe 16")
    spec_hash = _hash(value.get("spec_hash"), "spec_hash")
    template_hash = _hash(value.get("template_hash"), "template_hash")
    raw_cases = value.get("cases")
    if not isinstance(raw_cases, list):
        raise VerificationProgramError("verification plan cases must be a list")
    cases = tuple(_parse_case(item) for item in raw_cases)
    ids = tuple(case.id for case in cases)
    if len(set(ids)) != len(ids):
        raise VerificationProgramError("verification case ids must be unique")
    if ids != tuple(sorted(ids)):
        raise VerificationProgramError("verification case ids must be sorted")
    _validate_dependencies(cases)
    hashed = {
        "version": 1,
        "target_frappe_major": 16,
        "spec_hash": spec_hash,
        "cases": [_case_json(case) for case in cases],
    }
    if sha256(canonical_json(hashed)).hexdigest() != template_hash:
        raise VerificationProgramError("template_hash does not bind the verification plan")
    return VerificationProgram(1, 16, spec_hash, template_hash, cases)


def compile_case(case: ProgramCase) -> VerificationRequest:
    """Lower a validated semantic case to the closed request union."""

    if case.suite == "backend":
        return BackendVerificationRequest(case)
    if case.suite == "permissions":
        return PermissionVerificationRequest(case)
    if case.suite == "rest":
        return RestVerificationRequest(case)
    raise VerificationProgramError(f"unsupported verification suite {case.suite!r}")


def execute_verification_program(program: VerificationProgram, probe: VerificationProbe) -> VerificationRunReport:
    """Execute cases against an injected probe, skipping failed dependencies.

    The probe receives no free-form command, URL, credential, or document
    identity.  Template symbols such as ``created`` and ``Task User`` remain
    opaque data for the trusted provider to resolve.
    """

    results: dict[str, CaseResult] = {}
    for case in program.cases:
        if any(results[dependency].status != "passed" for dependency in case.depends_on):
            results[case.id] = CaseResult(case.id, case.expected, None, "skipped", "dependency_not_passed")
            continue
        try:
            observation = probe.probe(compile_case(case))
        except Exception:  # Provider detail may contain credentials or server diagnostics.
            results[case.id] = CaseResult(case.id, case.expected, None, "failed", "probe_error")
            continue
        if not isinstance(observation, VerificationObservation):
            results[case.id] = CaseResult(case.id, case.expected, None, "failed", "invalid_observation")
            continue
        status = "passed" if observation.outcome == case.expected else "failed"
        results[case.id] = CaseResult(
            case.id, case.expected, observation.outcome, status,
            "expected_outcome" if status == "passed" else "unexpected_outcome",
        )
    return VerificationRunReport(program.template_hash, tuple(results[case.id] for case in program.cases))


def _parse_case(value: Any) -> ProgramCase:
    if not isinstance(value, Mapping):
        raise VerificationProgramError("verification cases must be objects")
    identifier = _text(value.get("id"), "case id")
    suite = _text(value.get("suite"), "case suite")
    if suite not in _SUITE_OPERATIONS:
        raise VerificationProgramError(f"unsupported verification suite {suite!r}")
    operation = _text(value.get("operation"), "case operation")
    if operation not in _SUITE_OPERATIONS[suite]:
        raise VerificationProgramError(f"unsupported {suite} operation {operation!r}")
    expected = _text(value.get("expected"), "case expected outcome")
    if expected not in _EXPECTED[suite]:
        raise VerificationProgramError(f"unsupported {suite} expected outcome {expected!r}")
    actor = _text(value.get(_ACTOR), "case actor")
    entity = _text(value.get("entity"), "case entity")
    inputs = value.get("inputs")
    if not isinstance(inputs, Mapping):
        raise VerificationProgramError("case inputs must be an object")
    dependencies = value.get("depends_on", ())
    if not isinstance(dependencies, list) or any(not isinstance(item, str) or not item for item in dependencies):
        raise VerificationProgramError("case dependencies must be a list of non-empty ids")
    if len(set(dependencies)) != len(dependencies):
        raise VerificationProgramError("case dependencies must be unique")
    _validate_case_inputs(suite, operation, expected, actor, inputs)
    return ProgramCase(identifier, suite, entity, operation, actor, expected, dict(inputs), tuple(dependencies))


def _validate_case_inputs(suite: str, operation: str, expected: str, actor: str, inputs: Mapping[str, Any]) -> None:
    """Keep templates declarative while fail-closing unknown semantic shapes."""

    if suite == "permissions":
        _exact_keys(inputs, {"permission"})
        _equal(inputs["permission"], operation, "permission case operation must match its input")
        return
    if suite == "rest":
        if actor == "anonymous":
            if operation != "list" or expected != "authentication_required":
                raise VerificationProgramError("anonymous REST cases may only assert list authentication")
            _exact_keys(inputs, {"method"})
            _equal(inputs["method"], "GET", "anonymous REST list must use GET")
            return
        required = {
            "list": {"method", "permission"}, "read": {"method", "permission"},
            "create": {"method", "permission"}, "update": {"method", "permission"},
            "delete": {"method", "permission"}, "list_filtered": {"method", "filter_field", "filter_operator", "filter_value"},
            "list_ordered": {"method", "order_by", "order_direction"},
            "list_paginated": {"method", "page_start", "page_length"},
            "invalid_payload": {"method", "omitted_required_field"},
            "normalized_validation_error": {"method", "omitted_required_field"},
        }[operation]
        _exact_keys(inputs, required)
        if operation in {"invalid_payload", "normalized_validation_error"}:
            _equal(inputs["method"], "POST", "REST validation probes must use POST")
            omitted = inputs["omitted_required_field"]
            if not isinstance(omitted, str) or not _FIELD.fullmatch(omitted):
                raise VerificationProgramError("REST validation probe must omit a safe field identifier")
        return
    required = {
        "create_valid": {"payload", "fixture_entities"}, "read_created": {"record"},
        "update_valid": {"record", "changes"}, "create_missing_required": {"omitted_field"},
        "create_duplicate_unique": {"field", "value"}, "supported_field_type": {"field", "field_type", "value"},
        "select_allowed": {"field", "value"}, "select_rejected": {"field", "value"},
        "link_valid": {"field", "target_entity", "fixture"}, "link_missing": {"field", "target_entity", "fixture"},
    }[operation]
    _exact_keys(inputs, required)


def _validate_dependencies(cases: tuple[ProgramCase, ...]) -> None:
    ids = {case.id for case in cases}
    for case in cases:
        if any(dependency not in ids for dependency in case.depends_on):
            raise VerificationProgramError(f"case {case.id!r} has an unknown dependency")
    visiting: set[str] = set()
    visited: set[str] = set()
    by_id = {case.id: case for case in cases}

    def visit(identifier: str) -> None:
        if identifier in visiting:
            raise VerificationProgramError("verification case dependencies must not contain a cycle")
        if identifier in visited:
            return
        visiting.add(identifier)
        for dependency in by_id[identifier].depends_on:
            visit(dependency)
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in ids:
        visit(identifier)


def _case_json(case: ProgramCase) -> dict[str, Any]:
    return {
        "id": case.id, "suite": case.suite, "entity": case.entity,
        "operation": case.operation, "actor": case.actor, "expected": case.expected,
        "inputs": dict(case.inputs), "depends_on": list(case.depends_on),
    }


def _exact_keys(value: Mapping[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise VerificationProgramError("verification case inputs do not match the operation contract")


def _hash(value: Any, label: str) -> str:
    text = _text(value, label)
    if len(text) != _HASH_LENGTH or any(character not in "0123456789abcdef" for character in text):
        raise VerificationProgramError(f"{label} must be a SHA-256 digest")
    return text


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise VerificationProgramError(f"{label} must be a non-empty string")
    return value


def _equal(actual: Any, expected: Any, message: str) -> None:
    if actual != expected:
        raise VerificationProgramError(message)
