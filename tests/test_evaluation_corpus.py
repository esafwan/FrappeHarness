from __future__ import annotations

import pytest

from frappe_harness.evaluation_corpus import (
    CorpusError,
    Prediction,
    corpus_from_mapping,
    load_corpus,
    score_predictions,
)


def test_bundled_v1_corpus_is_complete_and_data_only() -> None:
    corpus = load_corpus()
    assert corpus.schema_version == 1
    assert len(corpus.cases) >= 20
    assert {case.category for case in corpus.cases} == {
        "supported", "ambiguity", "unsupported", "malformed_name", "permissions",
        "link_cycle", "safe_change", "destructive_change",
    }
    assert all("bench" not in case.prompt.lower() and "rm -rf" not in case.prompt.lower() for case in corpus.cases)


def test_perfect_predictions_score_deterministically() -> None:
    corpus = load_corpus()
    predictions = [Prediction(case.id, case.expected.decision, case.expected.reason_codes) for case in corpus.cases]
    report = score_predictions(corpus, predictions)
    assert report.total_cases == len(corpus.cases)
    assert report.decision_accuracy == 1.0
    assert report.reason_precision == 1.0
    assert report.reason_recall == 1.0
    assert report.unsafe_admission_rate == 0.0
    assert not report.missing_case_ids


def test_missing_and_unsafe_predictions_are_scored_fail_closed() -> None:
    corpus = load_corpus()
    destructive = next(case for case in corpus.cases if case.id == "DST-001")
    report = score_predictions(corpus, [Prediction(destructive.id, "accept", ("optional_field_added",))])
    assert len(report.missing_case_ids) == len(corpus.cases) - 1
    scored = next(score for score in report.case_scores if score.case_id == destructive.id)
    assert not scored.decision_correct
    assert scored.unsafe_admitted
    assert report.unsafe_admission_rate > 0


def test_duplicate_and_unknown_predictions_are_not_silently_accepted() -> None:
    corpus = load_corpus()
    with pytest.raises(CorpusError, match="duplicate prediction"):
        score_predictions(corpus, [Prediction("SUP-001", "accept"), Prediction("SUP-001", "accept")])
    report = score_predictions(corpus, [Prediction("not-a-case", "reject")])
    assert report.unknown_case_ids == ("not-a-case",)


def test_corpus_validation_rejects_unsafe_admission_and_missing_category() -> None:
    raw = {
        "schema_version": 1, "corpus_id": "x", "description": "x",
        "cases": [
            {"id": f"A-{index}", "category": "supported", "prompt": "x", "expected": {"decision": "accept", "reason_codes": []}}
            for index in range(20)
        ],
    }
    with pytest.raises(CorpusError, match="every V1 evaluation category"):
        corpus_from_mapping(raw)

    raw["cases"] = [
        {"id": f"A-{index}", "category": category, "prompt": "x", "expected": {"decision": decision, "reason_codes": []}}
        for index, (category, decision) in enumerate((
            ("supported", "accept"), ("ambiguity", "clarify"), ("unsupported", "accept"),
            ("malformed_name", "reject"), ("permissions", "reject"), ("link_cycle", "reject"),
            ("safe_change", "accept"), ("destructive_change", "reject"),
        ) * 3)
    ]
    with pytest.raises(CorpusError, match="unsafe category"):
        corpus_from_mapping(raw)
