import hashlib
import json
from pathlib import Path

import pytest

from frappe_harness.operator_bundle import (
    OperatorInputBundleError,
    bundle_from_mapping,
    bundle_json,
    load_operator_input_bundle,
)
from frappe_harness.run_store import LockTarget, RunIntent, RunRecord, RunState


def _bundle(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    refs: dict[str, dict[str, str]] = {}
    for kind in ("spec", "plan", "preflight", "compilation"):
        path = tmp_path / f"{kind}.json"
        path.write_text(json.dumps({"kind": kind}, sort_keys=True), encoding="utf-8")
        refs[kind] = {
            "kind": kind,
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    return tmp_path / "operator.json", {
        "schema_version": 1,
        "parent_run_id": "run-parent-1",
        "target": {"bench": "/bench", "site": "site.local", "app": "task_tracker"},
        "provider_id": "frappe-native",
        **refs,
        "approval": {"approver": "operator", "approved": True, "rationale": "confirmed"},
    }


def _v2_bundle(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    source, raw = _bundle(tmp_path)
    raw["schema_version"] = 2
    raw.pop("compilation")
    successor: dict[str, dict[str, str]] = {}
    for field, kind in (
        ("confirmed_spec", "spec"),
        ("confirmed_plan", "plan"),
        ("confirmed_compilation", "compilation"),
    ):
        path = tmp_path / f"successor-{field}.json"
        path.write_text(json.dumps({"kind": kind, "version": 2}, sort_keys=True), encoding="utf-8")
        successor[field] = {
            "kind": kind,
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    raw["successor"] = successor
    return source, raw


def test_operator_bundle_is_strict_and_digest_bound(tmp_path: Path):
    source, raw = _bundle(tmp_path)
    source.write_text(json.dumps(raw), encoding="utf-8")
    loaded = load_operator_input_bundle(source)
    assert loaded.parent_run_id == "run-parent-1"
    assert loaded.target.app == "task_tracker"
    assert json.loads(bundle_json(loaded))["spec"]["kind"] == "spec"

    loaded.spec.path.write_text('{"changed":true}', encoding="utf-8")
    with pytest.raises(OperatorInputBundleError, match="digest mismatch"):
        loaded.verify_references()


@pytest.mark.parametrize("extra", [
    {"shell": "bench migrate"},
    {"argv": ["bench", "migrate"]},
    {"password": "secret"},
    {"payload": {"arbitrary": True}},
])
def test_operator_bundle_rejects_raw_execution_or_secret_payload(tmp_path: Path, extra: dict[str, object]):
    _, raw = _bundle(tmp_path)
    raw.update(extra)
    with pytest.raises(OperatorInputBundleError, match="keys invalid"):
        bundle_from_mapping(raw)


def test_operator_bundle_rejects_reference_kind_and_symlink(tmp_path: Path):
    _, raw = _bundle(tmp_path)
    raw["plan"]["kind"] = "spec"  # type: ignore[index]
    with pytest.raises(OperatorInputBundleError, match="kind must be 'plan'"):
        bundle_from_mapping(raw)

    source, raw = _bundle(tmp_path)
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    raw["spec"]["path"] = str(link)  # type: ignore[index]
    raw["spec"]["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()  # type: ignore[index]
    with pytest.raises(OperatorInputBundleError, match="existing regular file"):
        bundle_from_mapping(raw)


def test_operator_bundle_rejects_non_boolean_approval(tmp_path: Path):
    _, raw = _bundle(tmp_path)
    raw["approval"]["approved"] = "true"  # type: ignore[index]
    with pytest.raises(OperatorInputBundleError, match="approved must be a JSON boolean"):
        bundle_from_mapping(raw)


def test_operator_bundle_must_bind_exact_persisted_parent_intent(tmp_path: Path):
    source, raw = _bundle(tmp_path)
    source.write_text(json.dumps(raw), encoding="utf-8")
    bundle = load_operator_input_bundle(source)
    parent = RunRecord(
        id=bundle.parent_run_id,
        target=bundle.target,
        state=RunState.RUNNING,
        created_at="now",
        updated_at="now",
        metadata={},
        intent=RunIntent(bundle.target, bundle.provider_id, bundle.spec.sha256, bundle.plan.sha256),
    )
    bundle.verify_parent_intent(parent)

    wrong_provider = RunRecord(
        id=parent.id,
        target=parent.target,
        state=parent.state,
        created_at=parent.created_at,
        updated_at=parent.updated_at,
        metadata=parent.metadata,
        intent=RunIntent(parent.target, "other-provider", parent.intent.spec_hash, parent.intent.plan_hash),
    )
    with pytest.raises(OperatorInputBundleError, match="provider"):
        bundle.verify_parent_intent(wrong_provider)

    wrong_hash = "0" * 64 if bundle.spec.sha256 != "0" * 64 else "1" * 64
    wrong_spec = RunRecord(
        id=parent.id,
        target=parent.target,
        state=parent.state,
        created_at=parent.created_at,
        updated_at=parent.updated_at,
        metadata=parent.metadata,
        intent=RunIntent(parent.target, parent.intent.provider, wrong_hash, parent.intent.plan_hash),
    )
    with pytest.raises(OperatorInputBundleError, match="spec reference"):
        bundle.verify_parent_intent(wrong_spec)


def test_operator_bundle_execution_admission_rejects_non_object_reference(tmp_path: Path):
    source, raw = _bundle(tmp_path)
    preflight = Path(raw["preflight"]["path"])  # type: ignore[index]
    preflight.write_text("[]", encoding="utf-8")
    raw["preflight"]["sha256"] = hashlib.sha256(preflight.read_bytes()).hexdigest()  # type: ignore[index]
    source.write_text(json.dumps(raw), encoding="utf-8")
    bundle = load_operator_input_bundle(source)
    parent = RunRecord(
        id=bundle.parent_run_id,
        target=bundle.target,
        state=RunState.RUNNING,
        created_at="now",
        updated_at="now",
        metadata={},
        intent=RunIntent(bundle.target, bundle.provider_id, bundle.spec.sha256, bundle.plan.sha256),
    )
    with pytest.raises(OperatorInputBundleError, match="preflight reference must contain a JSON object"):
        bundle.verify_execution_admission(parent)


def test_operator_bundle_execution_admission_rejects_unapproved_bundle(tmp_path: Path):
    source, raw = _bundle(tmp_path)
    raw["approval"]["approved"] = False  # type: ignore[index]
    source.write_text(json.dumps(raw), encoding="utf-8")
    bundle = load_operator_input_bundle(source)
    parent = RunRecord(
        id=bundle.parent_run_id,
        target=bundle.target,
        state=RunState.RUNNING,
        created_at="now",
        updated_at="now",
        metadata={},
        intent=RunIntent(bundle.target, bundle.provider_id, bundle.spec.sha256, bundle.plan.sha256),
    )
    with pytest.raises(OperatorInputBundleError, match="approval must be true"):
        bundle.verify_execution_admission(parent)


def test_schema_v2_adds_exact_successor_references_without_changing_parent_binding(tmp_path: Path):
    source, raw = _v2_bundle(tmp_path)
    source.write_text(json.dumps(raw), encoding="utf-8")
    bundle = load_operator_input_bundle(source)
    parent = RunRecord(
        id=bundle.parent_run_id,
        target=bundle.target,
        state=RunState.RUNNING,
        created_at="now",
        updated_at="now",
        metadata={},
        intent=RunIntent(bundle.target, bundle.provider_id, bundle.spec.sha256, bundle.plan.sha256),
    )

    bundle.verify_execution_admission(parent)

    assert bundle.successor is not None
    assert bundle.reconfirmed_spec_hash == bundle.successor.confirmed_spec.sha256
    assert bundle.successor_intent == RunIntent(
        bundle.target,
        bundle.provider_id,
        bundle.successor.confirmed_spec.sha256,
        bundle.successor.confirmed_plan.sha256,
    )
    serialized = json.loads(bundle_json(bundle))
    assert "compilation" not in serialized
    assert set(serialized["successor"]) == {
        "confirmed_spec",
        "confirmed_plan",
        "confirmed_compilation",
    }


def test_schema_v2_successor_shape_and_references_remain_fail_closed(tmp_path: Path):
    _, raw = _v2_bundle(tmp_path)
    raw["successor"]["payload"] = {"arbitrary": True}  # type: ignore[index]
    with pytest.raises(OperatorInputBundleError, match="successor keys invalid"):
        bundle_from_mapping(raw)

    source, raw = _v2_bundle(tmp_path)
    source.write_text(json.dumps(raw), encoding="utf-8")
    bundle = load_operator_input_bundle(source)
    assert bundle.successor is not None
    bundle.successor.confirmed_compilation.path.write_text('{"changed":true}', encoding="utf-8")
    with pytest.raises(OperatorInputBundleError, match="compilation reference digest mismatch"):
        bundle.verify_references()


def test_schema_v2_admission_rejects_a_non_successor_intent(tmp_path: Path):
    source, raw = _v2_bundle(tmp_path)
    successor = raw["successor"]  # type: ignore[assignment]
    successor["confirmed_spec"] = raw["spec"]  # type: ignore[index]
    successor["confirmed_plan"] = raw["plan"]  # type: ignore[index]
    source.write_text(json.dumps(raw), encoding="utf-8")
    bundle = load_operator_input_bundle(source)
    parent = RunRecord(
        id=bundle.parent_run_id,
        target=bundle.target,
        state=RunState.RUNNING,
        created_at="now",
        updated_at="now",
        metadata={},
        intent=RunIntent(bundle.target, bundle.provider_id, bundle.spec.sha256, bundle.plan.sha256),
    )

    with pytest.raises(OperatorInputBundleError, match="successor intent must differ"):
        bundle.verify_execution_admission(parent)
