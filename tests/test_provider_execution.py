from __future__ import annotations

import hashlib
import json
import pytest

from frappe_harness.frappectl_runner import FrappectlVerificationResult, RedactedProcessReceipt
from frappe_harness.provider_execution import (
    ProviderExecutionAdmissionError,
    RunBoundFrappectlMetadataAdapter,
    RunBoundFrappeVerificationAdapter,
)
from frappe_harness.frappe_rest_provider import FrappeRestResult, RestErrorKind, RestReceipt
from frappe_harness.frappe_verification_probe import VerificationFixtures
from frappe_harness.lifecycle_contract import LifecycleState
from frappe_harness.run_store import RunStore
from frappe_harness.test_templates import generate_verification_templates
from frappe_harness.verification_program import load_verification_program

from test_metadata_execution import _observed
from test_migration_execution import FakeProvider, _prepared_store


class FakeFrappectl:
    def __init__(self, payloads):
        self.payloads = payloads
        self.requests = []

    def inspect(self, request):
        self.requests.append(request)
        payload = dict(self.payloads[request.name])
        payload.setdefault("name", request.name)
        empty = hashlib.sha256(b"").hexdigest()
        return FrappectlVerificationResult(
            RedactedProcessReceipt(("frappectl",), 0, "", "", 0, empty, 0, empty), payload,
        )


def test_frappectl_metadata_adapter_binds_profile_and_passes_observed_metadata(tmp_path):
    spec, plan, compilation, store, run, approval, backup, checkpoint = _prepared_store(tmp_path)
    from frappe_harness.migration_execution import RunBoundMigrationExecutor
    from frappe_harness.schema_diff import classify_schema_diff

    RunBoundMigrationExecutor(store).execute(
        run.id, plan=plan, compilation=compilation, schema_diff=classify_schema_diff(spec, spec),
        approval_id=approval.id, backup_evidence_id=backup.id, checkpoint_evidence_id=checkpoint.id,
        provider=FakeProvider(),
    )
    fake = FakeFrappectl(_observed(plan))
    result = RunBoundFrappectlMetadataAdapter(
        store, profile="dev.local", adapter=fake,
    ).verify(run.id, plan=plan)
    assert result.report.matches
    assert len(fake.requests) == len(plan.operations)
    metadata_payload = store.get_lifecycle_evidence_payload(result.evidence.id)
    assert len(metadata_payload["provider_receipt_evidence_ids"]) == len(plan.operations)
    assert all("payload" not in store.get_lifecycle_evidence_payload(item) for item in metadata_payload["provider_receipt_evidence_ids"])


def test_frappectl_metadata_adapter_rejects_cross_site_profile(tmp_path):
    spec, plan, _compilation, store, run, _approval, _backup, _checkpoint = _prepared_store(tmp_path)
    with pytest.raises(ProviderExecutionAdmissionError, match="profile"):
        RunBoundFrappectlMetadataAdapter(
            store, profile="other.site", adapter=FakeFrappectl({}),
        ).verify(run.id, plan=plan)


def test_frappectl_metadata_adapter_binds_headless_environment_site(monkeypatch, tmp_path):
    spec, plan, compilation, store, run, approval, backup, checkpoint = _prepared_store(tmp_path)
    from frappe_harness.migration_execution import RunBoundMigrationExecutor
    from frappe_harness.schema_diff import classify_schema_diff

    RunBoundMigrationExecutor(store).execute(
        run.id, plan=plan, compilation=compilation, schema_diff=classify_schema_diff(spec, spec),
        approval_id=approval.id, backup_evidence_id=backup.id, checkpoint_evidence_id=checkpoint.id,
        provider=FakeProvider(),
    )
    monkeypatch.setenv("FRAPPE_SITE", run.target.site)
    result = RunBoundFrappectlMetadataAdapter(
        store, profile=None, adapter=FakeFrappectl(_observed(plan)),
    ).verify(run.id, plan=plan)
    assert result.report.matches


def test_rest_verification_adapter_requires_typed_actor_map(tmp_path):
    spec, plan, compilation, store, run, approval, backup, checkpoint = _prepared_store(tmp_path)
    templates = generate_verification_templates(spec)
    import json
    program = load_verification_program(json.loads(templates.artifacts["harness/verification-plan.json"]))
    with pytest.raises(ProviderExecutionAdmissionError, match="actor"):
        RunBoundFrappeVerificationAdapter(store, actors={}, fixtures=object()).execute_backend(
            run.id, program=program,
        )


def test_metadata_then_run_bound_82_case_backend_report_passes_backend_state(tmp_path):
    """Prove the real provider composition, not only the isolated store guard."""

    spec, plan, compilation, store, run, approval, backup, checkpoint = _prepared_store(tmp_path)
    from frappe_harness.migration_execution import RunBoundMigrationExecutor
    from frappe_harness.schema_diff import classify_schema_diff

    RunBoundMigrationExecutor(store).execute(
        run.id, plan=plan, compilation=compilation, schema_diff=classify_schema_diff(spec, spec),
        approval_id=approval.id, backup_evidence_id=backup.id, checkpoint_evidence_id=checkpoint.id,
        provider=FakeProvider(),
    )
    metadata = RunBoundFrappectlMetadataAdapter(
        store, profile="dev.local", adapter=FakeFrappectl(_observed(plan)),
    ).verify(run.id, plan=plan)
    assert metadata.report.matches
    assert store.get_lifecycle_snapshot(run.id).snapshot.record_for(LifecycleState.METADATA_VERIFIED).status.value == "passed"

    templates = generate_verification_templates(spec)
    program = load_verification_program(json.loads(templates.artifacts["harness/verification-plan.json"]))
    fixtures = VerificationFixtures(
        valid_payloads={
            "project": {"title": "_run_project", "active": True},
            "task": {"title": "_run_task", "project": {"fixture_entity": "project", "fixture_name": "_Test project"}, "status": "Open", "priority": "Medium"},
        },
        record_names={"project": "_run_fixture_project", "task": "_run_fixture_task"},
        update_payloads={"project": {"title": "_run_project_updated"}, "task": {"title": "_run_task_updated"}},
        delete_names={case.id: f"_run_delete_{index}" for index, case in enumerate(program.cases) if case.operation == "delete"},
    )

    class ActorProvider:
        def __init__(self, actor):
            self.actor = actor
            self.counter = 0

        def _result(self, status=200, payload=None, error=None):
            return FrappeRestResult(RestReceipt("live-shaped", status, 0, hashlib.sha256(b"").hexdigest()), payload, error)

        def _denied(self, doctype, operation):
            if self.actor == "unassigned":
                return True
            if self.actor == "Task User" and doctype == "Project" and operation in {"create", "update", "delete"}:
                return True
            return self.actor == "Task User" and operation == "delete"

        def list_documents(self, doctype, query=None):
            return self._result(403, error=RestErrorKind.AUTHENTICATION) if self.actor == "anonymous" else self._result()

        def get_document(self, doctype, name):
            return self._result(403, error=RestErrorKind.PERMISSION) if self._denied(doctype, "read") else self._result()

        def create_document(self, doctype, payload):
            if self._denied(doctype, "create"):
                return self._result(403, error=RestErrorKind.PERMISSION)
            required = ("title",) if doctype == "Project" else ("title", "project", "status", "priority")
            invalid = any(field not in payload for field in required) or payload.get("project") == "__missing__" or "__unsupported_option__" in payload.values()
            if invalid:
                return self._result(422, error=RestErrorKind.VALIDATION)
            self.counter += 1
            return self._result(201, {"data": {"name": f"_run_created_{self.actor}_{self.counter}"}})

        def update_document(self, doctype, name, payload):
            return self._result(403, error=RestErrorKind.PERMISSION) if self._denied(doctype, "update") else self._result()

        def delete_document(self, doctype, name):
            return self._result(403, error=RestErrorKind.PERMISSION) if self._denied(doctype, "delete") else self._result()

    actors = {actor: ActorProvider(actor) for actor in ("administrator", "Task Manager", "Task User", "unassigned", "anonymous")}
    result = RunBoundFrappeVerificationAdapter(store, actors=actors, fixtures=fixtures).execute_backend(run.id, program=program)

    assert result.report.passed
    assert len(result.report.results) == 82
    assert result.snapshot is not None
    assert result.snapshot.snapshot.record_for(LifecycleState.BACKEND_VERIFIED).status.value == "passed"
    assert store.get_verification_report(result.report_record.id) == result.report_record
