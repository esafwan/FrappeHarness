from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from frappe_harness.compiler import canonical_json
from frappe_harness.spec_io import load_project_spec
from frappe_harness.test_templates import generate_verification_templates
from frappe_harness.verification_program import (
    BackendVerificationRequest,
    PermissionVerificationRequest,
    RestVerificationRequest,
    VerificationObservation,
    VerificationProgramError,
    compile_case,
    execute_verification_program,
    load_verification_program,
)


def _plan():
    templates = generate_verification_templates(
        load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")
    )
    return json.loads(templates.artifacts["harness/verification-plan.json"])


def _rehash(plan):
    data = {key: plan[key] for key in ("version", "target_frappe_major", "spec_hash", "cases")}
    plan["template_hash"] = sha256(canonical_json(data)).hexdigest()


def test_hash_bound_golden_templates_load_into_deterministic_typed_program():
    first = load_verification_program(_plan())
    second = load_verification_program(_plan())

    assert first == second
    assert first.cases == tuple(sorted(first.cases, key=lambda case: case.id))
    assert isinstance(compile_case(first.cases[0]), (BackendVerificationRequest, PermissionVerificationRequest, RestVerificationRequest))


def test_program_execution_uses_typed_requests_and_preserves_symbolic_case_inputs():
    program = load_verification_program(_plan())
    seen = []

    class Probe:
        def probe(self, request):
            seen.append(request)
            return VerificationObservation(request.case.expected)

    report = execute_verification_program(program, Probe())

    assert report.passed
    assert len(seen) == len(program.cases)
    linked = next(request.case for request in seen if request.case.operation == "link_valid")
    assert linked.inputs["fixture"] == {"fixture_entity": "project", "fixture_name": "_Test project"}
    assert any(request.case.actor == "Task User" for request in seen)


def test_failed_case_skips_its_dependent_cases_without_calling_the_probe():
    program = load_verification_program(_plan())
    create = next(case for case in program.cases if case.id == "backend:task:create_valid:administrator")
    dependent = next(case for case in program.cases if create.id in case.depends_on)
    seen = []

    class Probe:
        def probe(self, request):
            seen.append(request.case.id)
            outcome = "validation_error" if request.case.id == create.id else request.case.expected
            return VerificationObservation(outcome)

    report = execute_verification_program(program, Probe())
    by_id = {result.case_id: result for result in report.results}

    assert by_id[create.id].code == "unexpected_outcome"
    assert by_id[dependent.id].status == "skipped"
    assert dependent.id not in seen
    assert not report.passed


@pytest.mark.parametrize(
    "mutate",
    [
        lambda plan: plan.__setitem__("template_hash", "0" * 64),
        lambda plan: plan["cases"][0].__setitem__("operation", "shell_escape"),
        lambda plan: plan["cases"][0].__setitem__("depends_on", ["missing:case"]),
    ],
)
def test_program_rejects_tampered_hash_unknown_operations_and_dangling_dependencies(mutate):
    plan = _plan()
    mutate(plan)

    with pytest.raises(VerificationProgramError):
        load_verification_program(plan)


def test_program_rejects_dependency_cycles_even_when_the_plan_is_rehashed():
    plan = _plan()
    first, second = plan["cases"][:2]
    first["depends_on"] = [second["id"]]
    second["depends_on"] = [first["id"]]
    _rehash(plan)

    with pytest.raises(VerificationProgramError, match="cycle"):
        load_verification_program(plan)


def test_program_rejects_rest_validation_templates_without_a_safe_concrete_omitted_field():
    plan = _plan()
    case = next(item for item in plan["cases"] if item["operation"] == "invalid_payload")
    case["inputs"]["omitted_required_field"] = "__unsafe_field__"
    _rehash(plan)

    with pytest.raises(VerificationProgramError, match="safe field"):
        load_verification_program(plan)
