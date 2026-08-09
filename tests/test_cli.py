import json
from pathlib import Path

import pytest

from frappe_harness.cli import main


def test_validate_and_compile_golden_fixture(tmp_path, capsys):
    fixture = Path(__file__).parents[1] / "fixtures" / "task_tracker.json"
    assert main(["validate", str(fixture)]) == 0
    validation = json.loads(capsys.readouterr().out)
    assert validation["valid"] is True

    output = tmp_path / "generated"
    assert main(["compile", str(fixture), "--output", str(output)]) == 0
    compiled = json.loads(capsys.readouterr().out)
    assert (output / "task_tracker" / "harness" / "ownership-manifest.json").is_file()
    assert (output / "task_tracker" / "harness" / "frontend-manifest.json").is_file()
    assert compiled["spec_hash"]


def test_compile_refuses_non_empty_directory(tmp_path, capsys):
    fixture = Path(__file__).parents[1] / "fixtures" / "task_tracker.json"
    output = tmp_path / "generated"
    output.mkdir()
    (output / "manual.txt").write_text("keep me", encoding="utf-8")

    assert main(["compile", str(fixture), "--output", str(output)]) == 2
    assert "refusing to write" in capsys.readouterr().err


def test_plan_emits_only_typed_semantic_operations(capsys):
    fixture = Path(__file__).parents[1] / "fixtures" / "task_tracker.json"

    assert main(["plan", str(fixture)]) == 0
    plan = json.loads(capsys.readouterr().out)

    assert plan["provider"] == "frappe-native"
    assert [operation["entity"] for operation in plan["operations"]] == ["project", "task"]


def test_route_status_reads_only_normalized_trace(tmp_path, capsys):
    from frappe_harness.route_trace import RouteTrace, trace_digest
    from frappe_harness.route_contracts import Route
    from frappe_harness.monitor_adapter import MonitorAction

    trace = RouteTrace("req_001", "dec_001", Route.SPEC_EXTRACT, True, True,
                       MonitorAction.CONTINUE, MonitorAction.CONTINUE, False, ("ok",), "0" * 64)
    trace = RouteTrace(**{**trace.__dict__, "trace_digest": trace_digest(trace)})
    path = tmp_path / "trace.json"
    path.write_text(json.dumps(trace.to_record()), encoding="utf-8")
    assert main(["route-status", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "request_id": "req_001", "decision_id": "dec_001", "route": "SPEC_EXTRACT",
        "policy_approved": True, "hard_stop": False, "next_action": "CONTINUE",
        "trace_digest": trace.trace_digest,
    }


def test_run_lifecycle_is_guarded(tmp_path, capsys):
    store = tmp_path / "runs.sqlite3"
    spec_hash = "a" * 64
    plan_hash = "b" * 64
    assert main([
        "run-start", "--store", str(store), "--bench", "/bench", "--site", "dev.local", "--app", "task_tracker",
        "--provider", "frappe-native", "--spec-hash", spec_hash, "--plan-hash", plan_hash,
    ]) == 0
    run_id = json.loads(capsys.readouterr().out)["run_id"]

    assert main([
        "approve", "--store", str(store), run_id, "--approver", "operator",
        "--provider", "frappe-native", "--spec-hash", spec_hash, "--plan-hash", plan_hash,
    ]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "approved"
    assert main(["run-transition", "--store", str(store), run_id, "running"]) == 0
    capsys.readouterr()
    assert main(["status", "--store", str(store), run_id]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "running"


def test_cli_rejects_an_approval_for_a_different_plan_hash(tmp_path, capsys):
    store = tmp_path / "runs.sqlite3"
    spec_hash = "a" * 64
    assert main([
        "run-start", "--store", str(store), "--bench", "/bench", "--site", "dev.local", "--app", "task_tracker",
        "--provider", "frappe-native", "--spec-hash", spec_hash, "--plan-hash", "b" * 64,
    ]) == 0
    run_id = json.loads(capsys.readouterr().out)["run_id"]

    assert main([
        "approve", "--store", str(store), run_id, "--approver", "operator",
        "--provider", "frappe-native", "--spec-hash", spec_hash, "--plan-hash", "c" * 64,
    ]) == 2
    assert "does not match" in capsys.readouterr().err


def test_cli_cannot_select_approved_as_a_generic_transition(tmp_path, capsys):
    store = tmp_path / "runs.sqlite3"
    assert main([
        "run-start", "--store", str(store), "--bench", "/bench", "--site", "dev.local", "--app", "task_tracker",
        "--provider", "frappe-native", "--spec-hash", "a" * 64, "--plan-hash", "b" * 64,
    ]) == 0
    run_id = json.loads(capsys.readouterr().out)["run_id"]

    with pytest.raises(SystemExit) as error:
        main(["run-transition", "--store", str(store), run_id, "approved"])

    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_cli_cannot_start_without_a_persisted_approval(tmp_path, capsys):
    store = tmp_path / "runs.sqlite3"
    assert main([
        "run-start", "--store", str(store), "--bench", "/bench", "--site", "dev.local", "--app", "task_tracker",
        "--provider", "frappe-native", "--spec-hash", "a" * 64, "--plan-hash", "b" * 64,
    ]) == 0
    run_id = json.loads(capsys.readouterr().out)["run_id"]

    assert main(["run-transition", "--store", str(store), run_id, "pending_approval"]) == 0
    capsys.readouterr()
    assert main(["run-transition", "--store", str(store), run_id, "running"]) == 2
    assert "cannot transition" in capsys.readouterr().err


def test_cli_records_exact_intent_bound_gate_approval(tmp_path, capsys):
    store = tmp_path / "runs.sqlite3"
    spec_hash = "a" * 64
    plan_hash = "b" * 64
    assert main([
        "run-start", "--store", str(store), "--bench", "/bench", "--site", "dev.local", "--app", "task_tracker",
        "--provider", "frappe-native", "--spec-hash", spec_hash, "--plan-hash", plan_hash,
    ]) == 0
    run_id = json.loads(capsys.readouterr().out)["run_id"]
    assert main([
        "approve", "--store", str(store), run_id, "--approver", "operator",
        "--provider", "frappe-native", "--spec-hash", spec_hash, "--plan-hash", plan_hash,
    ]) == 0
    capsys.readouterr()
    assert main([
        "gate-approve", "--store", str(store), run_id, "--gate", "CMP-GATE", "--approver", "operator",
        "--provider", "frappe-native", "--spec-hash", spec_hash, "--plan-hash", plan_hash,
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["gate"] == "CMP-GATE"
    assert result["decision"] == "approved"
    assert result["provider"] == "frappe-native"
    assert result["spec_hash"] == spec_hash
    assert result["plan_hash"] == plan_hash
    assert result["target"] == {"bench": "/bench", "site": "dev.local", "app": "task_tracker"}


def test_cli_rejects_gate_approval_with_wrong_intent(tmp_path, capsys):
    store = tmp_path / "runs.sqlite3"
    spec_hash = "a" * 64
    assert main([
        "run-start", "--store", str(store), "--bench", "/bench", "--site", "dev.local", "--app", "task_tracker",
        "--provider", "frappe-native", "--spec-hash", spec_hash, "--plan-hash", "b" * 64,
    ]) == 0
    run_id = json.loads(capsys.readouterr().out)["run_id"]
    assert main([
        "approve", "--store", str(store), run_id, "--approver", "operator",
        "--provider", "frappe-native", "--spec-hash", spec_hash, "--plan-hash", "b" * 64,
    ]) == 0
    capsys.readouterr()
    assert main([
        "gate-approve", "--store", str(store), run_id, "--gate", "CMP-GATE", "--approver", "operator",
        "--provider", "frappe-native", "--spec-hash", spec_hash, "--plan-hash", "c" * 64,
    ]) == 2
    assert "does not match" in capsys.readouterr().err


def test_cli_evaluates_gate_evidence_without_implicit_approval(tmp_path, capsys):
    store = tmp_path / "runs.sqlite3"
    spec_hash = "a" * 64
    plan_hash = "b" * 64
    assert main([
        "run-start", "--store", str(store), "--bench", "/bench", "--site", "dev.local", "--app", "task_tracker",
        "--provider", "frappe-native", "--spec-hash", spec_hash, "--plan-hash", plan_hash,
    ]) == 0
    run_id = json.loads(capsys.readouterr().out)["run_id"]
    assert main([
        "gate-evaluate", "--store", str(store), run_id, "--gate", "CMP-GATE",
        "--evidence-json", '{"compiler_golden": true}',
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["passed"] is False
    assert result["failures"] == ["missing_evidence:compiler_golden", "explicit_approval_required"]


def test_cli_evaluates_gate_with_same_run_durable_evidence_and_approval(tmp_path, capsys):
    store = tmp_path / "runs.sqlite3"
    spec_hash = "a" * 64
    plan_hash = "b" * 64
    assert main([
        "run-start", "--store", str(store), "--bench", "/bench", "--site", "dev.local", "--app", "task_tracker",
        "--provider", "frappe-native", "--spec-hash", spec_hash, "--plan-hash", plan_hash,
    ]) == 0
    run_id = json.loads(capsys.readouterr().out)["run_id"]
    assert main(["run-transition", "--store", str(store), run_id, "pending_approval"]) == 0
    capsys.readouterr()
    assert main([
        "approve", "--store", str(store), run_id, "--approver", "operator",
        "--provider", "frappe-native", "--spec-hash", spec_hash, "--plan-hash", plan_hash,
    ]) == 0
    capsys.readouterr()
    assert main(["run-transition", "--store", str(store), run_id, "running"]) == 0
    capsys.readouterr()
    from frappe_harness.run_store import RunStore
    durable = RunStore(store)
    evidence = durable.record_lifecycle_evidence(run_id, "compiler_golden", {"passed": True})
    assert main([
        "gate-approve", "--store", str(store), run_id, "--gate", "CMP-GATE", "--approver", "operator",
        "--provider", "frappe-native", "--spec-hash", spec_hash, "--plan-hash", plan_hash,
    ]) == 0
    capsys.readouterr()
    assert main([
        "gate-evaluate", "--store", str(store), run_id, "--gate", "CMP-GATE",
        "--evidence-json", json.dumps({"compiler_golden": evidence.reference}),
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["passed"] is True


def test_cli_release_resolve_derives_blocked_result_from_normalized_candidates(capsys):
    root = Path(__file__).parents[1] / "fixtures" / "evaluation" / "results"
    aggregate = root / "comparison_blocked_2026-08-09.json"
    candidates = [
        root / name for name in (
            "gemma4_corpus_2026-08-09.json",
            "gemini_3_5_flash_lite_corpus_2026-08-09.json",
            "gpt_oss_20b_corpus_2026-08-09.json",
            "qwen3_coder_next_corpus_2026-08-09.json",
        )
    ]
    argv = ["release-resolve", "--aggregate", str(aggregate)]
    for candidate in candidates:
        argv.extend(("--candidate", str(candidate)))
    assert main(argv) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["comparison_valid"] is True
    assert result["model_thresholds_passed"] is False
    assert "no_threshold_passing_candidate" in result["blockers"]
