from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from frappe_harness.bench_command_policy import InspectedBenchTarget
from frappe_harness.bench_runner import DockerContainerTarget
from frappe_harness.contracts import canonical_data
from frappe_harness.frappe_affected_checks import BrowserAttestation
from frappe_harness.frappe_rest_provider import FrappeRestProvider
from frappe_harness.live_chain_composition import compose_disposable_chain_inputs
from frappe_harness.operator_bundle import (
    OperatorArtifactRef,
    OperatorInputBundle,
    OperatorSuccessorInputRefs,
)
from frappe_harness.operator_input_loaders import compilation_envelope
from frappe_harness.provider_plan import build_metadata_plan, plan_json
from frappe_harness.run_store import LockTarget, RunStore
from frappe_harness.spec_io import load_project_spec
from frappe_harness.compiler import compile_project


TARGET = LockTarget("/workspace/development/frappe-harness-live-20260804", "frappe-harness-live-20260804.local", "task_tracker")


def _ref(tmp_path: Path, name: str, payload: object, *, kind: str | None = None) -> OperatorArtifactRef:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    return OperatorArtifactRef(kind or name, path, hashlib.sha256(path.read_bytes()).hexdigest())  # type: ignore[arg-type]


def test_composition_builds_exact_store_bound_provider_graph(tmp_path: Path):
    installed = load_project_spec(Path(__file__).parents[1] / "fixtures/task_tracker.json")
    confirmed = load_project_spec(Path(__file__).parents[1] / "fixtures/task_tracker_v2_reference_url.json")
    installed_ref = _ref(tmp_path, "installed-spec", canonical_data(installed), kind="spec")
    installed_plan = build_metadata_plan(installed)
    installed_plan_ref = _ref(tmp_path, "installed-plan", json.loads(plan_json(installed_plan)), kind="plan")
    preflight_payload = {
        "identity": {"bench": TARGET.bench, "site": TARGET.site, "app": TARGET.app},
        "frappe": {"major": 16, "version": "16.29.0"},
        "safety": {"developer_mode": True, "production_designated": False},
        "installed_apps": ["frappe", "task_tracker"],
        "tools": [{"name": "bench", "available": True, "version": "5.29.1"}],
        "disk": {"free_bytes": 2_000_000_000, "writable": True},
        "health_checks": [{"name": "database", "passed": True, "detail": None}],
        "ownership": {"source_clean": True, "ownership_conflict": False},
    }
    preflight_ref = _ref(tmp_path, "preflight", preflight_payload, kind="preflight")
    confirmed_ref = _ref(tmp_path, "confirmed-spec", canonical_data(confirmed), kind="spec")
    confirmed_plan = build_metadata_plan(confirmed)
    confirmed_plan_ref = _ref(tmp_path, "confirmed-plan", json.loads(plan_json(confirmed_plan)), kind="plan")
    confirmed_compilation_ref = _ref(tmp_path, "confirmed-compilation", compilation_envelope(compile_project(confirmed)), kind="compilation")
    bundle = OperatorInputBundle(
        parent_run_id="parent-run",
        target=TARGET,
        provider_id="frappe-native",
        spec=installed_ref,
        plan=installed_plan_ref,
        preflight=preflight_ref,
        compilation=None,
        approver="Administrator",
        approved=True,
        rationale="disposable test",
        schema_version=2,
        successor=OperatorSuccessorInputRefs(confirmed_ref, confirmed_plan_ref, confirmed_compilation_ref),
    )
    store = RunStore(tmp_path / "runs.sqlite3")
    composed = compose_disposable_chain_inputs(
        bundle,
        store=store,
        target=InspectedBenchTarget(TARGET.bench, TARGET.site, TARGET.app),
        container=DockerContainerTarget("frappe_docker_devcontainer-frappe-1"),
        rest=FrappeRestProvider("http://127.0.0.1:8000", None),
        task_name="TASK-1",
        expected_record={"name": "TASK-1"},
        browser_probe=lambda target: BrowserAttestation(target, "http://127.0.0.1:8000/task_tracker.html", True),
    )
    assert composed.affected_provider.store is store
    assert composed.artifact_deployer.inspected_target.bench_path == TARGET.bench


def test_composition_rejects_schema_v1(tmp_path: Path):
    with pytest.raises(ValueError, match="schema-v2"):
        compose_disposable_chain_inputs(
            object(), store=RunStore(tmp_path / "runs.sqlite3"), target=object(),
            container=DockerContainerTarget("frappe_docker_devcontainer-frappe-1"),
            rest=FrappeRestProvider("http://127.0.0.1:8000", None), task_name="TASK-1",
            expected_record={}, browser_probe=lambda target: None,
        )
