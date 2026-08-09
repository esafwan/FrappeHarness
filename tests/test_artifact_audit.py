from __future__ import annotations

import json
from pathlib import Path

from frappe_harness.artifact_audit import audit_artifacts, audit_compilation
from frappe_harness.compiler import compile_project
from frappe_harness.spec_io import load_project_spec


def _result():
    spec = load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")
    return compile_project(spec)


def _mutated_artifacts():
    result = _result()
    return dict(result.artifacts), dict(result.ownership_manifest)


def test_golden_compiler_output_passes_full_artifact_audit():
    assert audit_compilation(_result()).valid


def test_typed_compilation_requires_owned_frontend_manifest():
    artifacts, manifest = _mutated_artifacts()
    artifacts.pop("task_tracker/harness/frontend-manifest.json")
    manifest["owned_files"] = [
        entry for entry in manifest["owned_files"]
        if entry["path"] != "task_tracker/harness/frontend-manifest.json"
    ]

    report = audit_artifacts(artifacts, manifest)

    assert {issue.code for issue in report.issues} >= {
        "missing_required_artifact", "manifest_mismatch",
    }


def test_audit_rejects_invalid_json_path_escape_and_manifest_hash_drift():
    artifacts, manifest = _mutated_artifacts()
    artifacts["../escape.json"] = b"{"
    manifest["owned_files"].append({"path": "../escape.json", "sha256": "0" * 64})
    report = audit_artifacts(artifacts, manifest, expected_spec_hash="f" * 64)
    assert {issue.code for issue in report.issues} >= {"unsafe_path", "invalid_json", "spec_hash_mismatch"}


def test_audit_rejects_tampered_nested_frontend_manifest_and_hash():
    artifacts, manifest = _mutated_artifacts()
    path = "task_tracker/harness/frontend-manifest.json"
    frontend = json.loads(artifacts[path])
    frontend["roles"][0]["entities"][0]["fields"][0]["component"] = "untrusted-widget"
    artifacts[path] = json.dumps(frontend, sort_keys=True, separators=(",", ":")).encode()

    report = audit_artifacts(artifacts, manifest)

    assert {issue.code for issue in report.issues} >= {
        "frontend_manifest_hash_mismatch", "invalid_frontend_manifest", "ownership_hash_mismatch",
    }


def test_audit_rejects_unowned_or_tampered_compiler_artifacts():
    artifacts, manifest = _mutated_artifacts()
    path = "task_tracker/fixtures/roles.json"
    artifacts[path] = b"[]\n"
    report = audit_artifacts(artifacts, manifest)
    assert any(issue.code == "ownership_hash_mismatch" and issue.path == path for issue in report.issues)


def test_audit_rejects_secret_and_unsupported_hook_or_controller_api():
    artifacts, manifest = _mutated_artifacts()
    artifacts["task_tracker/task_tracker/hooks.py"] += b"\ndoc_events = {'*': {'validate': 'task_tracker.api.validate'}}\napi_key = 'abcdefgh12345678'\n"
    controller = "task_tracker/task_tracker/task_tracker/doctype/task/task.py"
    artifacts[controller] = b"from frappe import db\n\n\nclass Task(Document):\n    def validate(self):\n        db.sql('select 1')\n"
    report = audit_artifacts(artifacts, manifest)
    codes = {issue.code for issue in report.issues}
    assert {"secret_detected", "unsupported_hook", "unsupported_controller"} <= codes
    assert "unsupported_api" in codes


def test_audit_requires_exact_manifest_coverage_and_canonical_serialization():
    artifacts, manifest = _mutated_artifacts()
    artifacts["task_tracker/fixtures/roles.json"] = json.dumps([]).encode("utf-8")
    manifest["owned_files"] = manifest["owned_files"][:-1]
    report = audit_artifacts(artifacts, manifest)
    codes = {issue.code for issue in report.issues}
    assert {"manifest_mismatch", "noncanonical_json", "unowned_artifact"} <= codes


def test_audit_rejects_malformed_doctype_metadata_and_incomplete_package():
    artifacts, manifest = _mutated_artifacts()
    metadata = "task_tracker/task_tracker/task_tracker/doctype/task/task.json"
    artifacts[metadata] = b"{}\n"
    artifacts.pop("task_tracker/task_tracker/task_tracker/doctype/project/__init__.py")
    report = audit_artifacts(artifacts, manifest)
    codes = {issue.code for issue in report.issues}
    assert {"invalid_doctype_json", "invalid_doctype_fields", "invalid_doctype_permissions", "incomplete_doctype_artifact"} <= codes


def test_audit_rejects_code_in_package_initializer():
    artifacts, manifest = _mutated_artifacts()
    path = "task_tracker/task_tracker/task_tracker/doctype/task/__init__.py"
    artifacts[path] = b"from frappe import db\n"

    report = audit_artifacts(artifacts, manifest)

    assert "unsupported_package_initializer" in {issue.code for issue in report.issues}


def test_audit_permits_only_root_package_frappe_version_declaration():
    artifacts, manifest = _mutated_artifacts()
    root = "task_tracker/task_tracker/__init__.py"
    artifacts[root] = b'__version__ = "0.0.1"\n'

    assert audit_artifacts(artifacts, manifest).valid

    artifacts[root] = b'__version__ = "9.9.9"\n'
    assert "unsupported_package_initializer" in {issue.code for issue in audit_artifacts(artifacts, manifest).issues}


def test_audit_rejects_partial_or_mismatched_verification_bundle():
    artifacts, manifest = _mutated_artifacts()
    artifacts.pop("task_tracker/harness/tests/task.json")
    # The bundle is still structurally present; make the remaining entity file
    # contradict the plan to prove its binding is independently checked.
    project = json.loads(artifacts["task_tracker/harness/tests/project.json"])
    project["spec_hash"] = "f" * 64
    from frappe_harness.compiler import canonical_json
    artifacts["task_tracker/harness/tests/project.json"] = canonical_json(project)

    report = audit_artifacts(artifacts, manifest)
    assert "verification_entity_mismatch" in {issue.code for issue in report.issues}


def test_audit_rejects_tampered_hash_bound_verification_plan():
    artifacts, manifest = _mutated_artifacts()
    path = "task_tracker/harness/verification-plan.json"
    plan = json.loads(artifacts[path])
    plan["cases"][0]["operation"] = "shell_escape"
    from frappe_harness.compiler import canonical_json
    artifacts[path] = canonical_json(plan)

    report = audit_artifacts(artifacts, manifest)

    assert "invalid_verification_plan" in {issue.code for issue in report.issues}


def test_audit_rejects_verification_summary_that_does_not_describe_plan():
    artifacts, manifest = _mutated_artifacts()
    path = "task_tracker/harness/verification-summary.json"
    summary = json.loads(artifacts[path])
    summary["case_count"] += 1
    from frappe_harness.compiler import canonical_json
    artifacts[path] = canonical_json(summary)

    report = audit_artifacts(artifacts, manifest)

    assert "verification_summary_mismatch" in {issue.code for issue in report.issues}
