from __future__ import annotations

import pytest

from frappe_harness.lifecycle_contract import LifecycleState
from frappe_harness.migration_execution import MigrationProviderError, RunBoundMigrationExecutor
from frappe_harness.reconciliation_probe import MigrationInspectionFacts, RunnerBackedMigrationInspectionProbe, parse_migration_inspection_facts, reconcile_from_inspection
from frappe_harness.environment_preflight import UnsupportedEnvironmentFact
from frappe_harness.run_store import LockTarget

from test_metadata_execution import FakeProvider, _prepared_store
from frappe_harness.schema_diff import classify_schema_diff


def test_read_only_inspection_reconciles_then_allows_one_retry(tmp_path):
    spec, plan, compilation, store, run, approval, backup, checkpoint = _prepared_store(tmp_path)
    with pytest.raises(MigrationProviderError):
        RunBoundMigrationExecutor(store).execute(
            run.id, plan=plan, compilation=compilation, schema_diff=classify_schema_diff(spec, spec),
            approval_id=approval.id, backup_evidence_id=backup.id, checkpoint_evidence_id=checkpoint.id,
            provider=FakeProvider(explode=True),
        )

    class Probe:
        def inspect(self, target):
            return MigrationInspectionFacts(
                "bench:inspection:1", target, run.intent.spec_hash, run.intent.plan_hash,
            )

    result = reconcile_from_inspection(store, run.id, Probe())
    assert result.evidence.kind == "migration_reconciliation"
    assert result.snapshot.snapshot.record_for(LifecycleState.MIGRATION_APPLIED).status.value == "pending"
    retry = RunBoundMigrationExecutor(store).execute(
        run.id, plan=plan, compilation=compilation, schema_diff=classify_schema_diff(spec, spec),
        approval_id=approval.id, backup_evidence_id=backup.id, checkpoint_evidence_id=checkpoint.id,
        provider=FakeProvider(),
    )
    assert retry.snapshot.snapshot.record_for(LifecycleState.MIGRATION_APPLIED).status.value == "passed"


def test_read_only_inspection_cannot_reconcile_another_target(tmp_path):
    spec, plan, compilation, store, run, approval, backup, checkpoint = _prepared_store(tmp_path)

    class Probe:
        def inspect(self, target):
            return MigrationInspectionFacts(
                "bench:inspection:bad", LockTarget("/other", target.site, target.app),
                run.intent.spec_hash, run.intent.plan_hash,
            )

    with pytest.raises(Exception, match="run intent"):
        reconcile_from_inspection(store, run.id, Probe())


def test_confirmed_applied_inspection_cannot_open_a_retry_path(tmp_path):
    _spec, plan, _compilation, store, run, _approval, _backup, _checkpoint = _prepared_store(tmp_path)

    class Probe:
        def inspect(self, target):
            return MigrationInspectionFacts(
                "bench:inspection:applied", target, run.intent.spec_hash, run.intent.plan_hash,
                outcome="confirmed_applied",
            )

    with pytest.raises(Exception, match="post-migration evidence"):
        reconcile_from_inspection(store, run.id, Probe())


def test_failed_external_inspection_does_not_clear_interruption(tmp_path):
    _spec, plan, compilation, store, run, approval, backup, checkpoint = _prepared_store(tmp_path)
    with pytest.raises(MigrationProviderError):
        RunBoundMigrationExecutor(store).execute(
            run.id, plan=plan, compilation=compilation, schema_diff=classify_schema_diff(_spec, _spec),
            approval_id=approval.id, backup_evidence_id=backup.id, checkpoint_evidence_id=checkpoint.id,
            provider=FakeProvider(explode=True),
        )

    class Probe:
        def inspect(self, target):
            raise RuntimeError("read-only inspection unavailable")

    with pytest.raises(RuntimeError, match="inspection unavailable"):
        reconcile_from_inspection(store, run.id, Probe())
    assert store.get_lifecycle_snapshot(run.id).snapshot.record_for(LifecycleState.MIGRATION_APPLIED).status.value == "interrupted"


def test_inspection_payload_parser_is_strict_and_secret_free():
    payload = {
        "reference": "git:abc", "target": {"bench": "/bench", "site": "dev.local", "app": "task_tracker"},
        "spec_hash": "a" * 64, "plan_hash": "b" * 64, "outcome": "inspected",
    }
    facts = parse_migration_inspection_facts(payload)
    assert facts.target == LockTarget("/bench", "dev.local", "task_tracker")
    with pytest.raises(ValueError, match="unsupported"):
        parse_migration_inspection_facts({**payload, "secret": "do-not-retain"})
    with pytest.raises(ValueError, match="exactly bench/site/app"):
        parse_migration_inspection_facts({**payload, "target": {"bench": "/bench", "site": "dev.local", "app": "task_tracker", "token": "x"}})


def test_runner_backed_probe_is_read_only_and_explicit_when_unavailable():
    target = LockTarget("/bench", "dev.local", "task_tracker")
    probe = RunnerBackedMigrationInspectionProbe(lambda observed: {
        "reference": "bench:inspect:1", "target": {"bench": observed.bench, "site": observed.site, "app": observed.app},
        "spec_hash": "a" * 64, "plan_hash": "b" * 64,
    })
    assert probe.inspect(target).target == target
    def unavailable_runner(_target):
        raise FileNotFoundError()

    unavailable = RunnerBackedMigrationInspectionProbe(unavailable_runner)
    with pytest.raises(UnsupportedEnvironmentFact, match="inspection runner"):
        unavailable.inspect(target)
