from __future__ import annotations

import json

from frappe_harness.post_migration import verify_post_migration
from frappe_harness.schema_diff import classify_schema_diff
from frappe_harness.test_templates import generate_verification_templates
from frappe_harness.verification_program import VerificationObservation, load_verification_program

from test_metadata_execution import FakeProvider, _observed, _prepared_store
from frappe_harness.migration_execution import RunBoundMigrationExecutor


def _program(spec):
    templates = generate_verification_templates(spec)
    return load_verification_program(json.loads(templates.artifacts["harness/verification-plan.json"]))


def test_post_migration_verification_orders_metadata_before_backend(tmp_path):
    spec, plan, compilation, store, run, approval, backup, checkpoint = _prepared_store(tmp_path)
    RunBoundMigrationExecutor(store).execute(
        run.id, plan=plan, compilation=compilation, schema_diff=classify_schema_diff(spec, spec),
        approval_id=approval.id, backup_evidence_id=backup.id, checkpoint_evidence_id=checkpoint.id,
        provider=FakeProvider(),
    )
    seen = []

    class Probe:
        def probe(self, request):
            seen.append(request)
            return VerificationObservation(request.case.expected)

    result = verify_post_migration(
        store, run.id, plan=plan, observed_doctypes=_observed(plan),
        program=_program(spec), probe=Probe(),
    )
    assert result.metadata.report.matches
    assert result.backend is not None and result.backend.report.passed
    assert seen


def test_post_migration_mismatch_does_not_run_backend(tmp_path):
    spec, plan, compilation, store, run, approval, backup, checkpoint = _prepared_store(tmp_path)
    RunBoundMigrationExecutor(store).execute(
        run.id, plan=plan, compilation=compilation, schema_diff=classify_schema_diff(spec, spec),
        approval_id=approval.id, backup_evidence_id=backup.id, checkpoint_evidence_id=checkpoint.id,
        provider=FakeProvider(),
    )
    observed = _observed(plan)
    observed[next(iter(observed))]["fields"][0]["fieldtype"] = "Unknown"
    calls = 0

    class Probe:
        def probe(self, request):
            nonlocal calls
            calls += 1
            return VerificationObservation(request.case.expected)

    result = verify_post_migration(
        store, run.id, plan=plan, observed_doctypes=observed,
        program=_program(spec), probe=Probe(),
    )
    assert not result.metadata.report.matches
    assert result.backend is None
    assert calls == 0
