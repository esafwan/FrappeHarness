from dataclasses import replace
from pathlib import Path
import json
import json

from frappe_harness.artifact_audit import audit_compilation
from frappe_harness.compiler import compile_project
from frappe_harness.contracts import spec_hash, validate_project_spec
from frappe_harness.spec_io import load_project_spec


def test_compiler_manifest_uses_the_confirmed_canonical_spec_hash():
    spec = load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")

    assert validate_project_spec(spec).valid
    result = compile_project(spec)

    assert result.spec_hash == spec_hash(spec)
    assert result.ownership_manifest["spec_hash"] == spec_hash(spec)
    frontend = json.loads(result.text("task_tracker/harness/frontend-manifest.json"))
    assert frontend["spec_hash"] == spec_hash(spec)
    assert frontend["manifest_hash"]


def test_typed_compilation_includes_hash_bound_data_only_verification_templates():
    spec = load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")

    result = compile_project(spec)

    expected = {
        "task_tracker/harness/verification-plan.json",
        "task_tracker/harness/verification-summary.json",
        "task_tracker/harness/tests/project.json",
        "task_tracker/harness/tests/task.json",
    }
    assert expected <= set(result.artifacts)
    plan = json.loads(result.text("task_tracker/harness/verification-plan.json"))
    summary = json.loads(result.text("task_tracker/harness/verification-summary.json"))
    assert plan["spec_hash"] == result.spec_hash
    assert summary["template_hash"] == plan["template_hash"]
    assert audit_compilation(result).valid


def test_compilation_is_a_copyable_app_root_bundle_and_audit_uses_manifest_slug():
    spec = load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")
    result = compile_project(replace(spec, name="tracker_bundle"))

    paths = set(result.artifacts)
    assert all(path.startswith("tracker_bundle/") for path in paths)
    assert {
        "tracker_bundle/tracker_bundle/hooks.py",
        "tracker_bundle/tracker_bundle/__init__.py",
        "tracker_bundle/tracker_bundle/task_tracker/__init__.py",
        "tracker_bundle/tracker_bundle/task_tracker/doctype/__init__.py",
        "tracker_bundle/tracker_bundle/modules.txt",
        "tracker_bundle/fixtures/roles.json",
        "tracker_bundle/harness/spec.json",
        "tracker_bundle/harness/ownership-manifest.json",
        "tracker_bundle/tracker_bundle/task_tracker/doctype/task/task.json",
        "tracker_bundle/tracker_bundle/task_tracker/doctype/task/task.py",
        "tracker_bundle/tracker_bundle/task_tracker/doctype/task/__init__.py",
    } <= paths
    assert audit_compilation(result).valid
