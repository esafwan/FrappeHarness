from __future__ import annotations

import json
from pathlib import Path
import pytest

from frappe_harness.evaluation_corpus import load_corpus
from frappe_harness.release_evaluation import candidate_from_normalized_result, validate_comparison_artifact


def test_full_corpus_negative_result_is_normalized_without_raw_output():
    path = Path(__file__).parents[1] / "fixtures" / "evaluation" / "results" / "gpt_oss_20b_corpus_2026-08-09.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    result = candidate_from_normalized_result(raw, expected_corpus_id="frappe_harness_v1")
    assert result.model == "gpt-oss:20b"
    assert result.failure_code == "ollama_run_failed"
    assert result.unsafe_admission_count == 0
    assert not result.passed


def test_qwen_full_corpus_failure_is_not_treated_as_a_score():
    path = Path(__file__).parents[1] / "fixtures" / "evaluation" / "results" / "qwen3_coder_next_corpus_2026-08-09.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    result = candidate_from_normalized_result(raw, expected_corpus_id="frappe_harness_v1")
    assert result.model == "qwen3-coder-next:latest"
    assert result.score is None
    assert result.failure_code == "ollama_run_failed"


def test_release_comparison_artifact_is_explicitly_blocked():
    path = Path(__file__).parents[1] / "fixtures" / "evaluation" / "results" / "comparison_blocked_2026-08-09.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["corpus_id"] == "frappe_harness_v1"
    assert raw["threshold_passing_candidates"] == 0
    assert raw["recommended_model"] is None
    assert raw["release_eligible"] is False
    assert raw["unsafe_admission_count"] == 21
    assert raw["raw_output_retained"] is False


def test_release_comparison_artifact_matches_underlying_candidates():
    root = Path(__file__).parents[1] / "fixtures" / "evaluation" / "results"
    names = ("gemma4_corpus_2026-08-09.json", "gemini_3_5_flash_lite_corpus_2026-08-09.json", "gpt_oss_20b_corpus_2026-08-09.json", "qwen3_coder_next_corpus_2026-08-09.json")
    candidates = [candidate_from_normalized_result(json.loads((root / name).read_text()), expected_corpus_id="frappe_harness_v1") for name in names]
    aggregate = json.loads((root / "comparison_blocked_2026-08-09.json").read_text())
    comparison = validate_comparison_artifact(aggregate, candidates)
    assert comparison.recommended_model is None


def test_release_comparison_blocker_is_not_gemma_specific():
    root = Path(__file__).parents[1] / "fixtures" / "evaluation" / "results"
    candidate = candidate_from_normalized_result(
        json.loads((root / "gemma4_corpus_2026-08-09.json").read_text()),
        expected_corpus_id="frappe_harness_v1",
    )
    renamed = candidate.__class__("other-model:latest", candidate.corpus_id, candidate.score, candidate.thresholds, unsafe_admission_count=candidate.unsafe_admission_count)
    aggregate = {
        "schema_version": 1,
        "corpus_id": "frappe_harness_v1",
        "candidate_count": 1,
        "candidate_models": ["other-model:latest"],
        "threshold_passing_candidates": 0,
        "recommended_model": None,
        "release_eligible": False,
        "uncontrolled_command_paths_zero": False,
        "blockers": ["no_complete_threshold_passing_corpus_score", "complete_candidate_run_failed_all_release_thresholds"],
        "unsafe_admission_count": renamed.unsafe_admission_count,
        "raw_output_retained": False,
    }
    assert validate_comparison_artifact(aggregate, [renamed]).recommended_model is None


def test_release_comparison_artifact_rejects_forged_safety_total():
    root = Path(__file__).parents[1] / "fixtures" / "evaluation" / "results"
    names = ("gemma4_corpus_2026-08-09.json", "gemini_3_5_flash_lite_corpus_2026-08-09.json", "gpt_oss_20b_corpus_2026-08-09.json", "qwen3_coder_next_corpus_2026-08-09.json")
    candidates = [candidate_from_normalized_result(json.loads((root / name).read_text()), expected_corpus_id="frappe_harness_v1") for name in names]
    aggregate = json.loads((root / "comparison_blocked_2026-08-09.json").read_text())
    aggregate["unsafe_admission_count"] = 0
    with pytest.raises(ValueError, match="unsafe admission count"):
        validate_comparison_artifact(aggregate, candidates)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("schema_version", True, "schema version"),
        ("candidate_count", True, "candidate inventory"),
        ("threshold_passing_candidates", False, "passing count"),
        ("unsafe_admission_count", False, "unsafe admission count"),
    ),
)
def test_comparison_artifact_rejects_boolean_integer_claims(field, value, message):
    root = Path(__file__).parents[1] / "fixtures" / "evaluation" / "results"
    names = ("gemma4_corpus_2026-08-09.json", "gemini_3_5_flash_lite_corpus_2026-08-09.json", "gpt_oss_20b_corpus_2026-08-09.json", "qwen3_coder_next_corpus_2026-08-09.json")
    candidates = [candidate_from_normalized_result(json.loads((root / name).read_text()), expected_corpus_id="frappe_harness_v1") for name in names]
    aggregate = json.loads((root / "comparison_blocked_2026-08-09.json").read_text())
    aggregate[field] = value
    with pytest.raises(ValueError, match=message):
        validate_comparison_artifact(aggregate, candidates)


def test_release_comparison_artifact_rejects_forged_blockers():
    root = Path(__file__).parents[1] / "fixtures" / "evaluation" / "results"
    names = ("gemma4_corpus_2026-08-09.json", "gemini_3_5_flash_lite_corpus_2026-08-09.json", "gpt_oss_20b_corpus_2026-08-09.json", "qwen3_coder_next_corpus_2026-08-09.json")
    candidates = [candidate_from_normalized_result(json.loads((root / name).read_text()), expected_corpus_id="frappe_harness_v1") for name in names]
    aggregate = json.loads((root / "comparison_blocked_2026-08-09.json").read_text())
    aggregate["blockers"] = []
    with pytest.raises(ValueError, match="blockers"):
        validate_comparison_artifact(aggregate, candidates)


def test_gemma_full_corpus_score_import_preserves_unsafe_count():
    path = Path(__file__).parents[1] / "fixtures" / "evaluation" / "results" / "gemma4_corpus_2026-08-09.json"
    result = candidate_from_normalized_result(json.loads(path.read_text()), expected_corpus_id="frappe_harness_v1")
    assert result.score is not None
    assert result.score.total_cases == 28
    assert result.unsafe_admission_count == 13
    assert not result.passed


def test_scored_result_rejects_forged_threshold_report():
    path = Path(__file__).parents[1] / "fixtures" / "evaluation" / "results" / "gemma4_corpus_2026-08-09.json"
    raw = json.loads(path.read_text())
    raw["thresholds"] = {"passed": True, "failures": []}
    with pytest.raises(ValueError, match="threshold report does not match"):
        candidate_from_normalized_result(raw, expected_corpus_id="frappe_harness_v1")


def test_all_model_evidence_artifacts_attest_no_raw_output():
    root = Path(__file__).parents[1] / "fixtures" / "evaluation" / "results"
    for path in root.glob("*.json"):
        raw = json.loads(path.read_text())
        assert raw.get("raw_output_retained") is False, path.name


def test_normalized_result_rejects_raw_output_even_with_false_attestation():
    path = Path(__file__).parents[1] / "fixtures" / "evaluation" / "results" / "gpt_oss_20b_corpus_2026-08-09.json"
    raw = json.loads(path.read_text())
    raw["raw_output"] = "secret model response"
    with pytest.raises(ValueError, match="must not contain raw model output"):
        candidate_from_normalized_result(raw, expected_corpus_id="frappe_harness_v1")


def test_negative_normalized_result_rejects_stale_score_fields():
    path = Path(__file__).parents[1] / "fixtures" / "evaluation" / "results" / "gpt_oss_20b_corpus_2026-08-09.json"
    raw = json.loads(path.read_text())
    raw["score"] = None
    with pytest.raises(ValueError, match="must not contain score fields"):
        candidate_from_normalized_result(raw, expected_corpus_id="frappe_harness_v1")


def test_strict_normalized_import_rejects_case_ids_outside_bound_corpus():
    root = Path(__file__).parents[1]
    raw = json.loads((root / "fixtures/evaluation/results/gemma4_corpus_2026-08-09.json").read_text())
    raw["score"]["case_scores"][0]["case_id"] = "NOT-A-V1-CASE"
    with pytest.raises(ValueError, match="case ids do not match expected corpus"):
        candidate_from_normalized_result(
            raw,
            expected_corpus_id="frappe_harness_v1",
            expected_case_ids=(case.id for case in load_corpus().cases),
        )
