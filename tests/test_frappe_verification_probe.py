from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

from frappe_harness.frappe_rest_provider import FrappeRestResult, RestErrorKind, RestReceipt
from frappe_harness.frappe_verification_probe import (
    CreatedRecord,
    PROVIDER_FAILURE,
    UNMAPPABLE,
    FrappeVerificationProbe,
    VerificationFixtures,
)
from frappe_harness.spec_io import load_project_spec
from frappe_harness.test_templates import generate_verification_templates
from frappe_harness.verification_program import compile_case, execute_verification_program, load_verification_program


def _result(status=200, payload=None, error=None):
    return FrappeRestResult(RestReceipt("fake", status, 0, sha256(b"").hexdigest()), payload, error)


class FakeProvider:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def _next(self, operation, *args):
        self.calls.append((operation, args))
        return self.results.pop(0)

    def list_documents(self, doctype, query=None):
        return self._next("list", doctype, query)

    def get_document(self, doctype, name):
        return self._next("get", doctype, name)

    def create_document(self, doctype, payload):
        return self._next("create", doctype, payload)

    def update_document(self, doctype, name, payload):
        return self._next("update", doctype, name, payload)

    def delete_document(self, doctype, name):
        return self._next("delete", doctype, name)


def _program():
    spec = load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")
    plan = json.loads(generate_verification_templates(spec).artifacts["harness/verification-plan.json"])
    return load_verification_program(plan)


def _case(identifier):
    return compile_case(next(item for item in _program().cases if item.id == identifier))


def _fixtures():
    return VerificationFixtures(
        valid_payloads={
            "project": {"title": "probe project", "active": True},
            "task": {"title": "probe task", "project": {"fixture_entity": "project", "fixture_name": "_Test project"}, "status": "Open", "priority": "Medium"},
        },
        record_names={"project": "project-record", "task": "task-record"},
        update_payloads={"project": {"title": "updated"}, "task": {"title": "updated"}},
        delete_names={
            "permissions:project:delete:Task Manager": "permissions-project-manager-delete",
            "permissions:project:delete:Task User": "permissions-project-user-delete",
            "permissions:project:delete:unassigned": "permissions-project-unassigned-delete",
            "permissions:task:delete:Task Manager": "permissions-task-manager-delete",
            "permissions:task:delete:Task User": "permissions-task-user-delete",
            "permissions:task:delete:unassigned": "permissions-task-unassigned-delete",
            "rest:project:delete:Task Manager": "rest-project-manager-delete",
            "rest:project:delete:Task User": "rest-project-user-delete",
            "rest:task:delete:Task Manager": "rest-task-manager-delete",
            "rest:task:delete:Task User": "rest-task-user-delete",
        },
    )


def test_backend_created_symbolic_state_drives_read_and_update_without_leaking_document_payload():
    admin = FakeProvider(_result(201, {"data": {"name": "created-project"}}), _result(), _result())
    probe = FrappeVerificationProbe({"administrator": admin}, _fixtures())

    assert probe.probe(_case("backend:project:create_valid:administrator")).outcome == "success"
    assert probe.probe(_case("backend:project:read_created:administrator")).outcome == "success"
    assert probe.probe(_case("backend:project:update_valid:administrator")).outcome == "success"
    assert admin.calls[1] == ("get", ("Project", "created-project"))
    assert admin.calls[2] == ("update", ("Project", "created-project", {"title": "updated"}))
    assert probe.cleanup_records == (CreatedRecord("administrator", "project", "created-project"),)


def test_backend_valid_create_uses_run_specific_fixture_after_template_shape_validation():
    admin = FakeProvider(_result(201, {"data": {"name": "run-project"}}))
    fixtures = _fixtures()
    fixtures = VerificationFixtures(
        valid_payloads={**fixtures.valid_payloads, "project": {"title": "run-unique-project", "active": False}},
        record_names=fixtures.record_names,
        update_payloads=fixtures.update_payloads,
        delete_names=fixtures.delete_names,
    )
    probe = FrappeVerificationProbe({"administrator": admin}, fixtures)

    assert probe.probe(_case("backend:project:create_valid:administrator")).outcome == "success"
    assert admin.calls[0] == ("create", ("Project", {"title": "run-unique-project", "active": False}))


def test_incompatible_run_fixture_fails_closed_before_a_backend_mutation():
    admin = FakeProvider()
    fixtures = _fixtures()
    fixtures = VerificationFixtures(
        valid_payloads={**fixtures.valid_payloads, "project": {"title": 42, "active": True}},
        record_names=fixtures.record_names,
        update_payloads=fixtures.update_payloads,
        delete_names=fixtures.delete_names,
    )

    assert FrappeVerificationProbe({"administrator": admin}, fixtures).probe(_case("backend:project:create_valid:administrator")).outcome == UNMAPPABLE
    assert admin.calls == []


def test_incompatible_run_update_fixture_fails_closed_before_the_update():
    admin = FakeProvider(_result(201, {"data": {"name": "created-project"}}))
    fixtures = _fixtures()
    fixtures = VerificationFixtures(
        valid_payloads=fixtures.valid_payloads,
        record_names=fixtures.record_names,
        update_payloads={**fixtures.update_payloads, "project": {"title": 42}},
        delete_names=fixtures.delete_names,
    )
    probe = FrappeVerificationProbe({"administrator": admin}, fixtures)

    assert probe.probe(_case("backend:project:create_valid:administrator")).outcome == "success"
    assert probe.probe(_case("backend:project:update_valid:administrator")).outcome == UNMAPPABLE
    assert len(admin.calls) == 1


def test_backend_validation_and_link_symbol_resolution_use_only_closed_create_operation():
    admin = FakeProvider(
        _result(422, error=RestErrorKind.VALIDATION),
        _result(201, {"data": {"name": "linked-task"}}),
        _result(422, error=RestErrorKind.VALIDATION),
    )
    probe = FrappeVerificationProbe({"administrator": admin}, _fixtures())

    assert probe.probe(_case("backend:task:create_missing_required:title:administrator")).outcome == "validation_error"
    assert probe.probe(_case("backend:task:link_valid:project:administrator")).outcome == "success"
    assert probe.probe(_case("backend:task:link_missing:project:administrator")).outcome == "validation_error"
    assert admin.calls[1][1][1]["project"] == "project-record"


def test_permission_and_rest_cases_map_denials_without_recording_server_text():
    user = FakeProvider(_result(403, error=RestErrorKind.PERMISSION), _result(), _result(403, error=RestErrorKind.PERMISSION))
    probe = FrappeVerificationProbe({"Task User": user}, _fixtures())

    assert probe.probe(_case("permissions:project:create:Task User")).outcome == "denied"
    assert probe.probe(_case("rest:task:list:Task User")).outcome == "success"
    assert probe.probe(_case("rest:task:delete:Task User")).outcome == "permission_denied"
    assert user.calls[2] == ("delete", ("Task", "rest-task-user-delete"))


def test_permission_and_rest_deletes_require_distinct_exact_case_targets():
    user = FakeProvider(_result(403, error=RestErrorKind.PERMISSION), _result(403, error=RestErrorKind.PERMISSION))
    probe = FrappeVerificationProbe({"Task User": user}, _fixtures())

    assert probe.probe(_case("permissions:task:delete:Task User")).outcome == "denied"
    assert probe.probe(_case("rest:task:delete:Task User")).outcome == "permission_denied"
    assert user.calls == [
        ("delete", ("Task", "permissions-task-user-delete")),
        ("delete", ("Task", "rest-task-user-delete")),
    ]


def test_rest_list_variants_and_anonymous_authentication_use_typed_query_only():
    admin = FakeProvider(_result(), _result(), _result())
    anonymous = FakeProvider(_result(401, error=RestErrorKind.AUTHENTICATION))
    probe = FrappeVerificationProbe({"administrator": admin, "anonymous": anonymous}, _fixtures())

    assert probe.probe(_case("rest:project:list_filtered:administrator")).outcome == "success"
    assert probe.probe(_case("rest:project:list_ordered:administrator")).outcome == "success"
    assert probe.probe(_case("rest:project:list_paginated:administrator")).outcome == "success"
    assert probe.probe(_case("rest:project:list:anonymous")).outcome == "authentication_required"
    assert admin.calls[0][1][1].filters[0].field == "title"
    assert admin.calls[1][1][1].order_by == "title"
    assert admin.calls[2][1][1].start == 0
    assert admin.calls[2][1][1].limit == 2


def test_rest_validation_templates_use_a_safe_omitted_required_field_and_map_to_objective_outcomes():
    admin = FakeProvider(_result(422, error=RestErrorKind.VALIDATION), _result(422, error=RestErrorKind.VALIDATION))
    probe = FrappeVerificationProbe({"administrator": admin}, _fixtures())

    assert probe.probe(_case("rest:project:invalid_payload:administrator")).outcome == "validation_error"
    assert probe.probe(_case("rest:project:normalized_validation_error:administrator")).outcome == "normalized_error"
    assert "title" not in admin.calls[0][1][1]


def test_missing_fixture_still_fails_closed():
    probe = FrappeVerificationProbe({"administrator": FakeProvider()}, _fixtures())

    assert probe.probe(_case("permissions:project:delete:Task Manager")).outcome == UNMAPPABLE


def test_non_expected_provider_failure_does_not_invent_a_pass_or_retain_error_body():
    manager = FakeProvider(_result(500, payload={"errors": [{"message": "secret diagnostic"}]}, error=RestErrorKind.UNKNOWN))
    probe = FrappeVerificationProbe({"Task Manager": manager}, _fixtures())

    observation = probe.probe(_case("rest:project:create:Task Manager"))

    assert observation.outcome == PROVIDER_FAILURE
    assert "secret" not in repr(observation)


def test_every_current_hash_bound_case_has_a_closed_provider_mapping_with_complete_fixtures():
    class MatrixProvider:
        def __init__(self, actor):
            self.actor = actor

        def _denied(self, doctype, operation):
            if self.actor == "unassigned":
                return True
            if self.actor == "Task User" and doctype == "Project" and operation in {"create", "update", "delete"}:
                return True
            return self.actor == "Task User" and operation == "delete"

        def list_documents(self, doctype, query=None):
            return _result(401, error=RestErrorKind.AUTHENTICATION) if self.actor == "anonymous" else _result()

        def get_document(self, doctype, name):
            return _result(403, error=RestErrorKind.PERMISSION) if self._denied(doctype, "read") else _result()

        def create_document(self, doctype, payload):
            if self._denied(doctype, "create"):
                return _result(403, error=RestErrorKind.PERMISSION)
            invalid = not payload.get("title") or payload.get("project") == "__missing__" or "__unsupported_option__" in payload.values()
            return _result(422, error=RestErrorKind.VALIDATION) if invalid else _result(201, {"data": {"name": f"{doctype}-created"}})

        def update_document(self, doctype, name, payload):
            return _result(403, error=RestErrorKind.PERMISSION) if self._denied(doctype, "update") else _result()

        def delete_document(self, doctype, name):
            return _result(403, error=RestErrorKind.PERMISSION) if self._denied(doctype, "delete") else _result()

    actors = {actor: MatrixProvider(actor) for actor in ("administrator", "Task Manager", "Task User", "unassigned", "anonymous")}

    probe = FrappeVerificationProbe(actors, _fixtures())
    report = execute_verification_program(_program(), probe)

    assert report.passed
    assert len(report.results) == 82
    assert all(result.observed != UNMAPPABLE for result in report.results)
    assert probe.cleanup_records
    assert all(record.name.endswith("-created") for record in probe.cleanup_records)
