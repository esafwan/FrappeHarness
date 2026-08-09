"""Fail-closed entry point for the exact disposable V1-to-V2 evidence chain.

The current entry point performs only typed, read-only readiness probes.  It
will not create a ledger until concrete generated-artifact deployment and clean
source-checkpoint providers are implemented and composed with the existing
recovery, migration, affected-check, and safe-modification executors.

``--execute`` is intentionally an explicit operator switch. It admits only a
schema-v2 bundle with an existing parent, complete typed inputs, and a
process-local session; composition or provider failures are reported without
silently falling back to readiness.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3

import requests

from frappe_harness.bench_runner import DockerBenchRunner, DockerContainerTarget
from frappe_harness.live_chain_providers import GeneratedArtifactDeploymentProvider, CleanSourceCheckpointProvider
from frappe_harness.operator_bundle import OperatorInputBundleError, load_operator_input_bundle
from frappe_harness.operator_input_loaders import (
    load_confirmed_compilation,
    load_confirmed_plan,
    load_confirmed_preflight,
    load_confirmed_spec,
)
from frappe_harness.live_chain_composition import compose_disposable_chain_inputs
from frappe_harness.frappe_rest_provider import DocumentQuery, FrappeRestProvider, FrappeSessionCookie
from frappe_harness.live_modification_chain import RunBoundLiveModificationChainExecutor
from frappe_harness.run_store import LockTarget, RunStore, RunStoreError
from frappe_harness.live_modification_chain import (
    DISPOSABLE_APP,
    DISPOSABLE_BENCH,
    DISPOSABLE_CONTAINER,
    DISPOSABLE_SITE,
    DisposableLiveChainConfig,
    evaluate_live_chain_readiness,
)
from frappe_harness.assisted_admission import AssistedAdmissionError, require_live_admission
from frappe_harness.route_trace import parse_route_trace
from frappe_harness.commander_execution import CommanderRestClient, CommanderMigrationProvider, VERIFIED_REST_SUPPORTED_ACTION_KINDS, RegistryDisposableTargetAttestor
from frappe_harness.bench_registry_facts import BenchRegistryFactsRunner


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    parser.add_argument("--bench-path", required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--app", default=DISPOSABLE_APP)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--bundle", type=Path, help="strict reference-only operator input bundle for --execute")
    parser.add_argument("--task-name", help="existing Task name used for the fixed V2 preservation check")
    parser.add_argument("--execute", action="store_true", help="request the durable chain (currently fail-closed until composition is supplied)")
    parser.add_argument("--route-trace", type=Path, help="normalized assisted-flow trace required for --execute")
    parser.add_argument("--provider", choices=("frappe-native", "commander-spike"), default="frappe-native")
    args = parser.parse_args()

    try:
        config = DisposableLiveChainConfig(
            container=args.container,
            bench_path=args.bench_path,
            site=args.site,
            app=args.app,
            base_url=args.base_url,
            store_path=args.store,
            repository_root=Path(__file__).resolve().parents[1],
            # Read-only bundle admission may inspect an existing parent ledger;
            # readiness must continue to require a fresh external path.
            allow_existing_store=args.execute,
        )
        runner = DockerBenchRunner(DockerContainerTarget(config.container))
        if args.execute:
            # Do not silently downgrade an explicit execution request to a
            # readiness probe.  Until all run-bound inputs and concrete
            # providers are sourced from authoritative state, fail closed
            # without touching the target or creating an evidence ledger.
            blockers: list[str] = []
            bundle_error = None
            execution_started = False
            if args.bundle is None:
                blockers.extend(["operator_input_bundle_missing", "authoritative_executor_composition_unavailable"])
            elif args.route_trace is None:
                blockers.append("assisted_route_trace_missing")
            else:
                try:
                    assisted_trace = parse_route_trace(json.loads(args.route_trace.read_text(encoding="utf-8")))
                    require_live_admission(assisted_trace)
                    bundle = load_operator_input_bundle(args.bundle)
                    expected_target = config.inspected_target
                    if (bundle.target.bench, bundle.target.site, bundle.target.app) != (
                        expected_target.bench_path, expected_target.site_name, expected_target.app_slug,
                    ):
                        raise OperatorInputBundleError("operator bundle target does not match the disposable target")
                    if not config.store_path.exists():
                        raise OperatorInputBundleError("operator bundle parent ledger does not exist")
                    parent = RunStore(config.store_path).get_run(bundle.parent_run_id)
                    bundle.verify_execution_admission(parent)
                    if bundle.schema_version == 2 and bundle.successor is not None:
                        confirmed = load_confirmed_spec(bundle.successor.confirmed_spec)
                        confirmed_plan = load_confirmed_plan(bundle.successor.confirmed_plan, confirmed)
                        load_confirmed_compilation(bundle.successor.confirmed_compilation, confirmed)
                        load_confirmed_preflight(
                            bundle.preflight,
                            LockTarget(config.bench_path, config.site, config.app),
                        )
                        if confirmed_plan.spec_hash != bundle.successor.confirmed_spec.sha256:
                            raise OperatorInputBundleError("typed successor plan does not bind the bundle spec reference")
                        password = os.environ.get("FRAPPE_HARNESS_LIVE_ADMIN_PASSWORD")
                        if not password:
                            raise OperatorInputBundleError("process-local Administrator credential is missing")
                        session = requests.Session()
                        login = session.post(
                            args.base_url.rstrip("/") + "/api/method/login",
                            data={"usr": "Administrator", "pwd": password},
                            timeout=20,
                        )
                        login.raise_for_status()
                        cookie = "; ".join(f"{key}={value}" for key, value in session.cookies.get_dict().items())
                        if not cookie:
                            raise OperatorInputBundleError("Administrator login returned no session cookie")
                        rest = FrappeRestProvider(args.base_url, None, session_cookie=FrappeSessionCookie(cookie))
                        commander_client = CommanderRestClient(args.base_url, cookie=cookie, probe_existing=True) if args.provider == "commander-spike" else None
                        record_result = (
                            rest.get_document("Task", args.task_name)
                            if args.task_name
                            else rest.list_documents("Task", DocumentQuery(limit=1))
                        )
                        if not record_result.ok or not isinstance(record_result.payload, dict):
                            raise OperatorInputBundleError("typed Task lookup failed")
                        record = record_result.payload.get("data")
                        if isinstance(record, list):
                            record = record[0] if record else None
                        if not isinstance(record, dict) or not isinstance(record.get("name"), str):
                            raise OperatorInputBundleError("typed Task lookup returned no existing record")
                        task_name = record["name"]
                        expected_record = {
                            key: record.get(key)
                            for key in ("name", "title", "status", "project", "priority", "due_date")
                            if key in record
                        }
                        from live_v2_affected_evidence import _browser_probe
                        chain_store = RunStore(config.store_path)
                        inputs = compose_disposable_chain_inputs(
                            bundle,
                            store=chain_store,
                            target=expected_target,
                            container=DockerContainerTarget(config.container),
                            rest=rest,
                            task_name=task_name,
                            expected_record=expected_record,
                            browser_probe=lambda target: _browser_probe(target, args.base_url.rstrip("/"), password),
                            migration_provider=(CommanderMigrationProvider(
                                attestor=RegistryDisposableTargetAttestor(BenchRegistryFactsRunner(
                                    Path(__file__).resolve().parents[1] / "fixtures/live/registry_facts_2026-08-09.json"
                                )),
                                client=commander_client,
                                supported_action_kinds=VERIFIED_REST_SUPPORTED_ACTION_KINDS,
                            ) if commander_client else None),
                        )
                        execution_started = True
                        result = RunBoundLiveModificationChainExecutor(chain_store).execute(
                            bundle.parent_run_id,
                            installed=inputs.installed,
                            confirmed=inputs.confirmed,
                            reconfirmed_spec_hash=bundle.reconfirmed_spec_hash,
                            plan=inputs.plan,
                            compilation=inputs.compilation,
                            schema_diff=inputs.schema_diff,
                            successor_approval=inputs.approval,
                            preflight=inputs.preflight,
                            inspected_target=expected_target,
                            artifact_deployer=inputs.artifact_deployer,
                            recovery_executor=inputs.recovery_executor,
                            checkpoint_provider=inputs.checkpoint_provider,
                            migration_provider=inputs.migration_provider,
                            affected_provider=inputs.affected_provider,
                        )
                        payload = {
                            "ready": True,
                            "execution_requested": True,
                            "evidence_store_created": True,
                            "gate_approval_recorded": False,
                            "parent_run_id": bundle.parent_run_id,
                            "successor_run_id": result.successor.id,
                            "deployment_reference": result.deployment.reference,
                            "safe_modification_summary": result.modification.safe_modification.reference,
                            "container": DISPOSABLE_CONTAINER,
                            "bench_path": DISPOSABLE_BENCH,
                            "site": DISPOSABLE_SITE,
                            "app": DISPOSABLE_APP,
                            "store": str(config.store_path),
                        }
                        print(json.dumps(payload, sort_keys=True))
                        return 0
                except (
                    OperatorInputBundleError,
                    OSError,
                    RunStoreError,
                    sqlite3.DatabaseError,
                    requests.RequestException,
                    RuntimeError,
                    AssistedAdmissionError,
                    ValueError,
                ) as error:
                    bundle_error = str(error)
                    if getattr(error, "__cause__", None) is not None:
                        bundle_error += f"; cause: {error.__cause__}"
                    blockers.append("execution_or_composition_failed")
            payload = {
                "ready": False,
                "blockers": blockers,
                "diagnostic": (
                    bundle_error
                    or "--execute did not complete; no readiness downgrade or partial evidence is permitted"
                ),
                "evidence_store_created": execution_started,
                "gate_approval_recorded": False,
                "execution_requested": True,
                "container": DISPOSABLE_CONTAINER,
                "bench_path": DISPOSABLE_BENCH,
                "site": DISPOSABLE_SITE,
                "app": DISPOSABLE_APP,
                "store": str(config.store_path),
            }
            if bundle_error is not None:
                payload["diagnostic"] = bundle_error
            print(json.dumps(payload, sort_keys=True))
            return 2
        # These are capability checks only: constructors do not inspect or
        # mutate the target.  Composition availability is a code capability;
        # authoritative parent/bundle/session facts are still checked by the
        # explicit --execute path below and readiness never creates evidence.
        generated_available = clean_available = False
        try:
            GeneratedArtifactDeploymentProvider(config.inspected_target, runner)
            generated_available = True
            CleanSourceCheckpointProvider(config.inspected_target, runner)
            clean_available = True
        except (TypeError, ValueError, OSError):
            pass
        readiness = evaluate_live_chain_readiness(
            config,
            runner,
            generated_artifact_deployer_available=generated_available,
            clean_checkpoint_provider_available=clean_available,
            full_chain_executor_available=True,
            credential_available=bool(os.environ.get("FRAPPE_HARNESS_LIVE_ADMIN_PASSWORD")),
        )
        payload = readiness.as_dict() | {
            "container": DISPOSABLE_CONTAINER,
            "bench_path": DISPOSABLE_BENCH,
            "site": DISPOSABLE_SITE,
            "app": DISPOSABLE_APP,
            "store": str(config.store_path),
        }
    except (ValueError, OSError) as error:
        payload = {
            "ready": False,
            "blockers": ["invalid_operator_input"],
            "diagnostic": str(error),
            "evidence_store_created": False,
            "gate_approval_recorded": False,
        }
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
