from __future__ import annotations

import json
from pathlib import Path

from frappe_harness.spec_io import load_project_spec
from frappe_harness.test_templates import generate_verification_templates


def _spec():
    return load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")


def test_templates_are_stable_data_only_artifacts_bound_to_confirmed_spec():
    first = generate_verification_templates(_spec())
    second = generate_verification_templates(_spec())

    assert first.artifacts == second.artifacts
    assert first.template_hash == second.template_hash
    assert set(first.artifacts) == {
        "harness/tests/project.json", "harness/tests/task.json",
        "harness/verification-plan.json", "harness/verification-summary.json",
    }
    assert all(not path.endswith(".py") for path in first.artifacts)
    plan = json.loads(first.artifacts["harness/verification-plan.json"])
    assert plan["spec_hash"] == first.spec_hash
    assert plan["template_hash"] == first.template_hash
    validation_cases = [case for case in plan["cases"] if case["operation"] in {"invalid_payload", "normalized_validation_error"}]
    assert validation_cases
    assert all(case["inputs"] == {"method": "POST", "omitted_required_field": "title"} for case in validation_cases)


def test_templates_cover_required_select_link_permissions_and_rest_boundaries():
    result = generate_verification_templates(_spec())
    cases = {(case.entity, case.suite, case.operation, case.actor): case for case in result.cases}

    assert ("task", "backend", "create_missing_required", "administrator") in cases
    assert ("task", "backend", "select_rejected", "administrator") in cases
    link = cases[("task", "backend", "link_valid", "administrator")]
    assert link.inputs["target_entity"] == "project"
    assert cases[("task", "permissions", "delete", "Task User")].expected == "denied"
    assert cases[("task", "permissions", "delete", "Task Manager")].expected == "allowed"
    assert cases[("task", "rest", "list_paginated", "administrator")].inputs["page_length"] == 2
    assert cases[("task", "rest", "list", "anonymous")].expected == "authentication_required"
    rich_text = next(
        case for case in result.cases
        if case.entity == "task" and case.suite == "backend" and case.operation == "supported_field_type"
        and case.inputs["field"] == "description"
    )
    assert rich_text.inputs == {
        "field": "description", "field_type": "Text Editor", "value": "<p>_Test description</p>"
    }


def test_templates_emit_semantic_rest_actions_not_urls_commands_or_code():
    result = generate_verification_templates(_spec())
    rendered = b"".join(result.artifacts.values()).decode("utf-8")

    assert "bench" not in rendered
    assert "subprocess" not in rendered
    assert "frappe.client" not in rendered
    assert "/api/" not in rendered
    assert {case.inputs.get("method") for case in result.cases if case.suite == "rest"} >= {"GET", "POST", "PATCH", "DELETE"}
