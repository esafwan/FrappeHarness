from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from frappe_harness.compiler import compile_project
from frappe_harness.contracts import canonical_data, spec_hash
from frappe_harness.operator_bundle import OperatorArtifactRef, OperatorInputBundleError
from frappe_harness.operator_input_loaders import (
    OperatorTypedInputError,
    compilation_envelope,
    load_confirmed_compilation,
    load_confirmed_preflight,
    load_confirmed_plan,
    load_confirmed_spec,
)
from frappe_harness.provider_plan import build_metadata_plan, plan_hash_document, plan_json
from frappe_harness.spec_io import load_project_spec
from frappe_harness.run_store import LockTarget


def _write(path: Path, value: object) -> OperatorArtifactRef:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    return OperatorArtifactRef(path.stem, path, hashlib.sha256(path.read_bytes()).hexdigest())  # type: ignore[arg-type]


def test_typed_loaders_rebuild_and_bind_canonical_inputs(tmp_path: Path):
    spec = load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")
    spec_ref = _write(tmp_path / "spec.json", canonical_data(spec))
    loaded = load_confirmed_spec(spec_ref)
    assert spec_hash(loaded) == spec_ref.sha256

    plan = build_metadata_plan(loaded)
    plan_ref = _write(tmp_path / "plan.json", json.loads(plan_json(plan)))
    assert load_confirmed_plan(plan_ref, loaded) == plan

    compilation = compile_project(loaded)
    compilation_ref = _write(tmp_path / "compilation.json", compilation_envelope(compilation))
    assert load_confirmed_compilation(compilation_ref, loaded).spec_hash == compilation.spec_hash


def test_plan_hash_document_digest_is_the_semantic_plan_hash(tmp_path: Path):
    spec = load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")
    plan = build_metadata_plan(spec)
    ref = _write(tmp_path / "plan.json", plan_hash_document(plan))
    assert ref.sha256 == plan.plan_hash
    assert load_confirmed_plan(ref, spec) == plan


def test_typed_loader_rejects_digest_drift(tmp_path: Path):
    spec = load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")
    ref = _write(tmp_path / "spec.json", canonical_data(spec))
    ref.path.write_text(ref.path.read_text(encoding="utf-8").replace("Task Tracker", "Changed"), encoding="utf-8")
    with pytest.raises(OperatorInputBundleError, match="digest mismatch"):
        load_confirmed_spec(ref)


def test_compilation_loader_rejects_envelope_drift(tmp_path: Path):
    spec = load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")
    compilation = compile_project(spec)
    envelope = compilation_envelope(compilation)
    envelope["artifact_hashes"]["extra"] = "0" * 64  # type: ignore[index]
    ref = _write(tmp_path / "compilation.json", envelope)
    with pytest.raises(OperatorTypedInputError, match="deterministic compiler output"):
        load_confirmed_compilation(ref, spec)


def test_preflight_loader_requires_complete_exact_target_facts(tmp_path: Path):
    payload = {
        "identity": {"bench": "/bench", "site": "site.local", "app": "task_tracker"},
        "frappe": {"major": 16, "version": "16.29.0"},
        "safety": {"developer_mode": True, "production_designated": False},
        "installed_apps": ["frappe", "task_tracker"],
        "tools": [{"name": "bench", "available": True, "version": "5.29.1"}],
        "disk": {"free_bytes": 2_000_000_000, "writable": True},
        "health_checks": [{"name": "database", "passed": True, "detail": None}],
        "ownership": {"source_clean": True, "ownership_conflict": False},
    }
    ref = _write(tmp_path / "preflight.json", payload)
    target = LockTarget("/bench", "site.local", "task_tracker")
    assert load_confirmed_preflight(ref, target).identity.app == "task_tracker"
    with pytest.raises(OperatorTypedInputError, match="exact target"):
        load_confirmed_preflight(ref, LockTarget("/other", "site.local", "task_tracker"))
