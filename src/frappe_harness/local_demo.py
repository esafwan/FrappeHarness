"""Deterministic, no-I/O demo of the complete local lifecycle contract."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .compiler import CompilationResult, compile_project
from .contracts import ProjectSpec
from .environment_preflight import (
    DiskHealth, EnvironmentIdentity, EnvironmentPreflight, FrappeRuntime, HealthCheck, SiteSafety, ToolStatus,
)
from .frappe_verification_probe import VerificationFixtures
from .lifecycle_contract import LifecycleState
from .metadata_execution import RunBoundMetadataVerifier
from .migration_execution import MigrationProviderReceipt, RunBoundMigrationExecutor
from .provider_plan import MetadataPlan, build_metadata_plan
from .run_store import LockTarget, RunIntent, RunState, RunStore
from .runtime_lifecycle import RunLifecycleCoordinator
from .schema_diff import classify_schema_diff
from .test_templates import generate_verification_templates
from .verification_execution import RunBoundVerificationExecutor
from .verification_program import VerificationObservation, load_verification_program


@dataclass(frozen=True)
class DemoReceipt:
    run_id: str
    spec_hash: str
    plan_hash: str
    final_state: str
    lifecycle_states: tuple[str, ...]


class _DemoMigrationProvider:
    provider_id = "frappe-native"

    def apply(self, *, target: LockTarget, plan: MetadataPlan, compilation: CompilationResult) -> MigrationProviderReceipt:
        return MigrationProviderReceipt(
            f"demo:migration:{target.site}", 0,
            stdout_sha256=hashlib.sha256(b"demo migration").hexdigest(),
        )


class _PassingProbe:
    def probe(self, request):
        return VerificationObservation(request.case.expected)


def run_local_demo(store_path: str | Path) -> DemoReceipt:
    """Run all evidence-backed local states using only in-memory demo adapters."""
    fixture = Path(__file__).resolve().parents[2] / "fixtures" / "task_tracker.json"
    spec = _load_spec(fixture)
    plan = build_metadata_plan(spec)
    compilation = compile_project(spec)
    store = RunStore(store_path)
    target = LockTarget("/demo/bench", "demo.local", spec.name)
    run = store.create_run(RunIntent(target, plan.provider, plan.spec_hash, plan.plan_hash))
    store.transition(run.id, RunState.PENDING_APPROVAL)
    approval = store.record_approval(run.id, approver="local-demo", approved=True, intent=run.intent)
    store.transition(run.id, RunState.APPROVED)
    store.transition(run.id, RunState.RUNNING)
    coordinator = RunLifecycleCoordinator(store)
    coordinator.record_preflight(run.id, _preflight(target))
    coordinator.record_compilation(run.id, spec, compilation)
    coordinator.record_plan_approval(run.id, plan, approval.id)
    command = store.start_command(run.id, command="bench", arguments={"operation": "SiteBackup"})
    command = store.complete_command(command.id, exit_code=0)
    target = {"bench": run.target.bench, "site": run.target.site, "app": run.target.app}
    backup = store.record_lifecycle_evidence(run.id, "backup", {"recovery_receipt": "v1", "command_receipt_id": command.id, "operation": "SiteBackup", "target": target})
    checkpoint = store.record_lifecycle_evidence(run.id, "checkpoint", {"recovery_receipt": "v1", "provider_id": "source-checkpoint", "reference": "demo:checkpoint", "checkpoint": "demo", "target": target, "spec_hash": run.intent.spec_hash, "plan_hash": run.intent.plan_hash})
    diff = classify_schema_diff(spec, spec)
    RunBoundMigrationExecutor(store).execute(
        run.id, plan=plan, compilation=compilation, schema_diff=diff,
        approval_id=approval.id, backup_evidence_id=backup.id, checkpoint_evidence_id=checkpoint.id,
        provider=_DemoMigrationProvider(),
    )
    RunBoundMetadataVerifier(store).verify(run.id, plan=plan, observed_doctypes=_observed_metadata(plan))
    program = load_verification_program(json.loads(generate_verification_templates(spec).artifacts["harness/verification-plan.json"]))
    backend = RunBoundVerificationExecutor(store).execute_backend(run.id, program, _PassingProbe())
    frontend_hash = hashlib.sha256(compilation.artifacts["task_tracker/harness/frontend-manifest.json"]).hexdigest()
    coordinator.record_frontend_verification(run.id, manifest_hash=frontend_hash, scenario_count=1)
    coordinator.record_ready(run.id, receipt={"spec_hash": spec_hash_from(spec), "backend_report": backend.report_record.id})
    store.transition(run.id, RunState.SUCCEEDED)
    snapshot = store.get_lifecycle_snapshot(run.id)
    assert snapshot is not None
    return DemoReceipt(run.id, plan.spec_hash, plan.plan_hash, store.get_run(run.id).state.value, tuple(
        record.state.value for record in snapshot.snapshot.records
    ))


def _load_spec(path: Path) -> ProjectSpec:
    from .spec_io import load_project_spec
    return load_project_spec(path)


def spec_hash_from(spec: ProjectSpec) -> str:
    from .contracts import spec_hash
    return spec_hash(spec)


def _preflight(target: LockTarget) -> EnvironmentPreflight:
    return EnvironmentPreflight(
        EnvironmentIdentity(target.bench, target.site, target.app), FrappeRuntime(16, "16.0"),
        SiteSafety(True, False), (), (ToolStatus("bench", True), ToolStatus("frappectl", True)),
        DiskHealth(2_000_000_000, True), (HealthCheck("database", True), HealthCheck("site", True)),
    )


def _observed_metadata(plan: MetadataPlan) -> Mapping[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    entity_to_doctype = {operation.entity: operation.doctype for operation in plan.operations}
    for operation in plan.operations:
        fields = []
        for field in operation.fields:
            options: Any = field.frappe_options
            if field.link_target:
                options = entity_to_doctype[field.link_target]
            elif field.select_options:
                options = "\n".join(field.select_options)
            fields.append({
                "fieldname": field.name, "fieldtype": field.frappe_field_type or field.field_type,
                "reqd": 1 if field.required else 0, "unique": 1 if field.unique else 0,
                "default": field.default, "options": options,
            })
        result[operation.doctype] = {"name": operation.doctype, "fields": fields, "permissions": [
            {"role": permission.role, "read": permission.read, "create": permission.create,
             "write": permission.write, "delete": permission.delete, "permlevel": 0}
            for permission in operation.permissions
        ]}
    return result
