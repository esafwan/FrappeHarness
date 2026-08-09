"""Create an explicitly approved parent V1 ledger and schema-v2 bundle.

This tool only creates a parent control-plane record and reference artifacts;
it does not invoke Bench, REST, browser, migration, or deployment providers.
The caller must supply a complete, target-bound preflight document and an
explicit operator confirmation.
"""

from __future__ import annotations

import argparse
import json
from hashlib import sha256
from pathlib import Path

from frappe_harness.compiler import compile_project
from frappe_harness.contracts import canonical_data
from frappe_harness.environment_facts_io import facts_to_mapping
from frappe_harness.environment_preflight import (
    EnvironmentPreflightPolicy,
    normalize_environment_facts_payload,
    evaluate_environment_preflight,
)
from frappe_harness.operator_bundle import (
    OperatorArtifactRef,
    OperatorInputBundle,
    OperatorSuccessorInputRefs,
    bundle_json,
)
from frappe_harness.provider_plan import build_metadata_plan, plan_hash_document
from frappe_harness.run_store import LockTarget, RunIntent, RunState, RunStore
from frappe_harness.runtime_lifecycle import RunLifecycleCoordinator
from frappe_harness.spec_io import load_project_spec


def _write(directory: Path, name: str, payload: object, *, kind: str | None = None) -> OperatorArtifactRef:
    path = directory / name
    path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    artifact_kind = kind or name.split(".", 1)[0]
    return OperatorArtifactRef(artifact_kind, path, sha256(path.read_bytes()).hexdigest())  # type: ignore[arg-type]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--preflight", required=True, type=Path)
    parser.add_argument("--bench", required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--app", default="task_tracker")
    parser.add_argument("--provider", choices=("frappe-native", "commander-spike"), default="frappe-native")
    parser.add_argument("--approver", required=True)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    if not args.confirm:
        raise SystemExit("--confirm is required to create the explicit parent approval")
    target = LockTarget(args.bench, args.site, args.app)
    facts = normalize_environment_facts_payload(json.loads(args.preflight.read_text(encoding="utf-8")))
    preflight = facts_to_mapping(facts)
    eligibility = evaluate_environment_preflight(
        facts_to_preflight(facts),
        policy=EnvironmentPreflightPolicy(required_tools=("bench",)),
        intent=RunIntent(target, "frappe-native", "0" * 64, "0" * 64),
    )
    if not eligibility.eligible:
        raise SystemExit("preflight is ineligible: " + ",".join(eligibility.failure_codes))
    root = Path(__file__).resolve().parents[1]
    installed = load_project_spec(root / "fixtures" / "task_tracker.json")
    confirmed = load_project_spec(root / "fixtures" / "task_tracker_v2_reference_url.json")
    parent_plan = build_metadata_plan(installed, provider=args.provider)
    successor_plan = build_metadata_plan(confirmed, provider=args.provider)
    parent_compilation = compile_project(installed)
    successor_compilation = compile_project(confirmed)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    spec_ref = _write(args.output_dir, "spec.json", canonical_data(installed), kind="spec")
    plan_ref = _write(args.output_dir, "plan.json", plan_hash_document(parent_plan), kind="plan")
    preflight_ref = _write(args.output_dir, "preflight.json", preflight, kind="preflight")
    successor_spec_ref = _write(args.output_dir, "confirmed_spec.json", canonical_data(confirmed), kind="spec")
    successor_plan_ref = _write(args.output_dir, "confirmed_plan.json", plan_hash_document(successor_plan), kind="plan")
    from frappe_harness.operator_input_loaders import compilation_envelope
    successor_compilation_ref = _write(args.output_dir, "confirmed_compilation.json", compilation_envelope(successor_compilation), kind="compilation")
    store = RunStore(args.store)
    intent = RunIntent(target, parent_plan.provider, parent_plan.spec_hash, parent_plan.plan_hash)
    parent = store.create_run(intent)
    store.transition(parent.id, RunState.PENDING_APPROVAL)
    approval = store.record_approval(parent.id, approver=args.approver, approved=True, intent=intent, rationale="explicit disposable V1 parent confirmation")
    store.transition(parent.id, RunState.APPROVED)
    store.transition(parent.id, RunState.RUNNING)
    lifecycle = RunLifecycleCoordinator(store)
    lifecycle.record_preflight(
        parent.id,
        facts_to_preflight(facts),
        policy=EnvironmentPreflightPolicy(required_tools=("bench",)),
    )
    lifecycle.record_compilation(parent.id, installed, parent_compilation)
    lifecycle.record_plan_approval(parent.id, parent_plan, approval.id)
    bundle = OperatorInputBundle(
        parent_run_id=parent.id,
        target=target,
        provider_id=successor_plan.provider,
        spec=spec_ref,
        plan=plan_ref,
        preflight=preflight_ref,
        compilation=None,
        approver=args.approver,
        approved=True,
        rationale="explicit disposable V2 successor confirmation",
        schema_version=2,
        successor=OperatorSuccessorInputRefs(successor_spec_ref, successor_plan_ref, successor_compilation_ref),
    )
    bundle_path = args.output_dir / "operator_bundle.json"
    bundle_path.write_text(bundle_json(bundle), encoding="utf-8")
    print(json.dumps({"parent_run_id": parent.id, "store": str(args.store), "bundle": str(bundle_path), "preflight": str(preflight_ref.path)}, sort_keys=True))
    return 0


def facts_to_preflight(facts):
    from frappe_harness.environment_preflight import EnvironmentPreflight, SiteSafety
    return EnvironmentPreflight(
        facts.identity, facts.frappe,
        SiteSafety(facts.developer_mode, facts.production_designated),
        facts.installed_apps, facts.tools, facts.disk, facts.health_checks,
    )


if __name__ == "__main__":
    raise SystemExit(main())
