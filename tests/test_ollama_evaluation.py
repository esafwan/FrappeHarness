from frappe_harness.evaluation_corpus import load_corpus
from frappe_harness.ollama_evaluation import CorpusPredictionError, OllamaCorpusPredictionAdapter, evaluate_corpus_with_ollama, parse_corpus_prediction
from frappe_harness.ollama_gateway import OllamaGatewayConfig
import pytest


def test_prediction_parser_rejects_extra_keys_and_unknown_case():
    case = load_corpus().cases[0]
    good = {"case_id": case.id, "decision": "accept", "reason_codes": []}
    assert parse_corpus_prediction(good, case).case_id == case.id
    with pytest.raises(CorpusPredictionError):
        parse_corpus_prediction({**good, "extra": 1}, case)
    with pytest.raises(CorpusPredictionError):
        parse_corpus_prediction({**good, "case_id": "unknown"}, case)


def test_ollama_corpus_runner_normalizes_generation_failure_without_raw_output():
    corpus = load_corpus()
    config = OllamaGatewayConfig(model="gemma4:latest")

    def broken(_config, _prompt):
        raise RuntimeError("model unavailable")

    result = evaluate_corpus_with_ollama(corpus, OllamaCorpusPredictionAdapter(config, broken))
    assert result.failure_code == "ollama_prediction_failed"
    assert result.score is None
    assert result.unsafe_admission_count == 0


def test_ollama_corpus_runner_retries_one_schema_failure_per_case():
    corpus = load_corpus()
    calls = {}

    def flaky(_config, prompt):
        case_id = prompt.split("case_id must be ", 1)[1].split("; decision", 1)[0].strip(" '")
        calls[case_id] = calls.get(case_id, 0) + 1
        if calls[case_id] == 1:
            return {"case_id": case_id, "decision": "not-a-decision", "reason_codes": []}
        case = next(item for item in corpus.cases if item.id == case_id)
        return {"case_id": case.id, "decision": case.expected.decision, "reason_codes": list(case.expected.reason_codes)}

    result = evaluate_corpus_with_ollama(corpus, OllamaCorpusPredictionAdapter(generator=flaky))
    assert result.score is not None
    assert all(count == 2 for count in calls.values())


def test_schema_retry_feedback_does_not_include_gold_labels():
    corpus = load_corpus()
    prompts = []
    counts = {}

    def malformed_then_valid(_config, prompt):
        prompts.append(prompt)
        case_id = prompt.split("case_id must be ", 1)[1].split("; decision", 1)[0].strip(" '")
        counts[case_id] = counts.get(case_id, 0) + 1
        if counts[case_id] == 1:
            return {"case_id": case_id, "decision": "bad", "reason_codes": []}
        return {"case_id": case_id, "decision": "clarify", "reason_codes": []}

    evaluate_corpus_with_ollama(corpus, OllamaCorpusPredictionAdapter(generator=malformed_then_valid))
    retry_prompts = [prompt for prompt in prompts if "Previous attempt failed" in prompt]
    assert retry_prompts
    for prompt in retry_prompts:
        assert "expected" not in prompt.lower()
        case_id = prompt.split("case_id must be ", 1)[1].split("; decision", 1)[0].strip(" '")
        case = next(item for item in corpus.cases if item.id == case_id)
        for reason_code in case.expected.reason_codes:
            assert reason_code.lower() not in prompt.lower()


def test_corpus_prompt_contains_decision_safety_rubric_without_gold_labels():
    case = load_corpus().cases[0]
    prompts = []

    def capture(_config, prompt):
        prompts.append(prompt)
        return {"case_id": case.id, "decision": "accept", "reason_codes": []}

    OllamaCorpusPredictionAdapter(generator=capture).predict(case)
    assert "fully specified, supported by the V1 vocabulary, and safe" in prompts[0]
    assert "Do not include explanations outside the JSON object" in prompts[0]
    assert "expected" not in prompts[0].lower()


@pytest.mark.parametrize("case_index", range(len(load_corpus().cases)))
def test_every_corpus_prompt_excludes_adjudicated_reason_labels(case_index):
    """The evaluator must never leak case gold labels into model input."""
    corpus = load_corpus()
    case = corpus.cases[case_index]
    prompts = []

    def capture(_config, prompt):
        prompts.append(prompt)
        return {"case_id": case.id, "decision": "clarify", "reason_codes": []}

    OllamaCorpusPredictionAdapter(generator=capture).predict(case)
    assert len(prompts) == 1
    prompt = prompts[0].lower()
    assert "expected" not in prompt
    for reason_code in case.expected.reason_codes:
        assert reason_code.lower() not in prompt
