from __future__ import annotations

from frappe_harness.evaluation_corpus import load_corpus
from frappe_harness.gemini_evaluation import GeminiCorpusConfig, GeminiCorpusPredictionAdapter, evaluate_corpus_with_gemini


def test_gemini_adapter_uses_exact_corpus_schema_without_raw_output():
    corpus = load_corpus()
    calls = []

    def generator(config, prompt):
        calls.append(prompt)
        case_id = corpus.cases[0].id
        return {"case_id": case_id, "decision": "accept", "reason_codes": []}

    adapter = GeminiCorpusPredictionAdapter(GeminiCorpusConfig(model="test"), generator=generator)
    first = corpus.cases[0]
    assert adapter.predict(first).decision == "accept"
    assert len(calls) == 1


def test_gemini_corpus_failure_is_normalized_to_code_only_result():
    corpus = load_corpus()
    result = evaluate_corpus_with_gemini(
        corpus,
        GeminiCorpusPredictionAdapter(GeminiCorpusConfig(model="test"), generator=lambda *_: {"invalid": True}),
    )
    assert result.failure_code == "gemini_prediction_failed"
    assert result.score is None and result.thresholds is None
