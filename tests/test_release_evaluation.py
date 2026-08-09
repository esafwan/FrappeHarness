from __future__ import annotations

import json
from pathlib import Path

import pytest

from frappe_harness.evaluation_corpus import Prediction, load_corpus, score_predictions
from frappe_harness.release_evaluation import (
    CandidateEvaluation, ModelEvaluation, ReleaseThresholds, candidate_from_normalized_result, check_thresholds, compare_candidates, evaluate_candidate,
    select_default_model, resolve_release_evidence, validate_comparison_artifact,
)


def _perfect():
    corpus = load_corpus()
    return score_predictions(corpus, [Prediction(case.id, case.expected.decision, case.expected.reason_codes) for case in corpus.cases])


def test_perfect_score_passes_release_thresholds():
    report = check_thresholds(_perfect())
    assert report.passed
    assert not report.failures


def test_unsafe_or_missing_evidence_fails_closed():
    corpus = load_corpus()
    report = score_predictions(corpus, [Prediction("DST-001", "accept")])
    checked = check_thresholds(report)
    assert not checked.passed
    assert {"unsafe_admission_rate", "missing_case_ids"} <= set(checked.failures)


def test_default_selection_is_deterministic_and_requires_passing_models():
    corpus = load_corpus()
    score = _perfect()
    selected = select_default_model([
        ModelEvaluation("gemma4:latest", score), ModelEvaluation("other", score),
    ])
    assert selected is not None
    assert selected.model == "other"
    unsafe = score_predictions(corpus, [Prediction("DST-001", "accept")])
    assert select_default_model([ModelEvaluation("unsafe", unsafe)]) is None


def test_candidate_runner_records_gemma_negative_run_without_model_output():
    corpus = load_corpus()
    failed = evaluate_candidate(corpus, "gemma4:latest", failure_code="strict_schema_violation")
    assert not failed.passed
    assert failed.failure_code == "strict_schema_violation"
    assert failed.score is None
    assert compare_candidates([failed]).recommended_model is None


def test_release_evidence_resolution_derives_blockers_from_normalized_comparison():
    root = Path(__file__).parents[1] / "fixtures" / "evaluation" / "results"
    names = (
        "gemma4_corpus_2026-08-09.json",
        "gemini_3_5_flash_lite_corpus_2026-08-09.json",
        "gpt_oss_20b_corpus_2026-08-09.json",
        "qwen3_coder_next_corpus_2026-08-09.json",
    )
    candidates = [candidate_from_normalized_result(json.loads((root / name).read_text()), expected_corpus_id="frappe_harness_v1") for name in names]
    aggregate = json.loads((root / "comparison_blocked_2026-08-09.json").read_text())
    resolved = resolve_release_evidence(aggregate, candidates, uncontrolled_command_paths_zero=False)
    assert resolved.comparison_valid
    assert not resolved.model_thresholds_passed
    assert resolved.corpus_coverage_complete
    assert not resolved.unsafe_admissions_zero
    assert "no_threshold_passing_candidate" in resolved.blockers
    assert "uncontrolled_command_paths_not_proven_zero" in resolved.blockers


def test_comparison_requires_explicit_command_path_evidence():
    root = Path(__file__).parents[1] / "fixtures" / "evaluation" / "results"
    aggregate = json.loads((root / "comparison_blocked_2026-08-09.json").read_text())
    names = (
        "gemma4_corpus_2026-08-09.json",
        "gemini_3_5_flash_lite_corpus_2026-08-09.json",
        "gpt_oss_20b_corpus_2026-08-09.json",
        "qwen3_coder_next_corpus_2026-08-09.json",
    )
    candidates = [candidate_from_normalized_result(json.loads((root / name).read_text()), expected_corpus_id="frappe_harness_v1") for name in names]
    missing = dict(aggregate)
    missing.pop("uncontrolled_command_paths_zero")
    with pytest.raises(ValueError, match="uncontrolled_command_paths_zero"):
        validate_comparison_artifact(missing, candidates)


def test_release_resolution_rejects_conflicting_caller_safety_flag():
    root = Path(__file__).parents[1] / "fixtures" / "evaluation" / "results"
    aggregate = json.loads((root / "comparison_blocked_2026-08-09.json").read_text())
    names = (
        "gemma4_corpus_2026-08-09.json",
        "gemini_3_5_flash_lite_corpus_2026-08-09.json",
        "gpt_oss_20b_corpus_2026-08-09.json",
        "qwen3_coder_next_corpus_2026-08-09.json",
    )
    candidates = [candidate_from_normalized_result(json.loads((root / name).read_text()), expected_corpus_id="frappe_harness_v1") for name in names]
    resolved = resolve_release_evidence(aggregate, candidates, uncontrolled_command_paths_zero=True)
    assert not resolved.comparison_valid
    assert resolved.blockers == ("uncontrolled_command_path_evidence_mismatch",)
def test_candidate_comparison_is_sorted_and_selects_only_threshold_passing_runs():
    corpus = load_corpus()
    perfect = evaluate_candidate(
        corpus, "other", [Prediction(case.id, case.expected.decision, case.expected.reason_codes) for case in corpus.cases],
    )
    malformed = evaluate_candidate(corpus, "gemma4:latest", [Prediction("SUP-001", "accept"), Prediction("SUP-001", "accept")])
    comparison = compare_candidates([malformed, perfect])
    assert tuple(item.model for item in comparison.evaluations) == ("gemma4:latest", "other")
    assert comparison.recommended_model == "other"
    assert malformed.failure_code == "invalid_predictions"


def test_candidate_comparison_rejects_mixed_corpus_evidence():
    corpus = load_corpus()
    perfect = evaluate_candidate(
        corpus, "one", [Prediction(case.id, case.expected.decision, case.expected.reason_codes) for case in corpus.cases],
    )
    other_corpus = CandidateEvaluation("two", "evaluation-v2", perfect.score, perfect.thresholds)
    with pytest.raises(ValueError, match="same corpus"):
        compare_candidates([perfect, other_corpus])


def test_successful_candidate_retains_unsafe_admission_count():
    corpus = load_corpus()
    result = evaluate_candidate(corpus, "gemma4:latest", [
        Prediction(case.id, "accept") for case in corpus.cases
    ])
    assert result.unsafe_admission_count == sum(
        case.expected.decision == "reject" for case in corpus.cases
    )


def test_normalized_gemma_negative_result_import_is_secret_free():
    result = candidate_from_normalized_result({
        "schema_version": 1,
        "model": "gemma4:latest",
        "corpus_id": "frappe_harness_v1",
        "status": "escalated",
        "failure_code": "strict_schema_violation",
        "unsafe_admission_count": 0,
        "raw_output_retained": False,
    }, expected_corpus_id="frappe_harness_v1")
    assert result.failure_code == "strict_schema_violation"
    assert not result.passed


def test_normalized_negative_result_retains_unsafe_admission_count():
    result = candidate_from_normalized_result({
        "schema_version": 1, "model": "gemma4:latest", "corpus_id": "frappe_harness_v1",
        "status": "failed", "failure_code": "unsafe", "unsafe_admission_count": 2,
        "raw_output_retained": False,
    }, expected_corpus_id="frappe_harness_v1")
    assert result.unsafe_admission_count == 2


def test_normalized_gpt_oss_negative_result_is_comparable_but_not_passing():
    path = Path(__file__).parents[1] / "fixtures" / "evaluation" / "results" / "gpt_oss_20b_2026-08-09.json"
    result = candidate_from_normalized_result(json.loads(path.read_text()), expected_corpus_id="frappe_harness_v1")
    assert result.model == "gpt-oss:20b"
    assert result.failure_code == "repair_attempts_exhausted"
    assert not result.passed
