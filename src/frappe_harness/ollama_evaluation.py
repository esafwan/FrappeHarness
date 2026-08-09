"""Optional, data-only Ollama corpus prediction adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .evaluation_corpus import DECISIONS, EvaluationCase, EvaluationCorpus, Prediction
from .ollama_gateway import OllamaGatewayConfig, _ollama_generate
from .release_evaluation import CandidateEvaluation, evaluate_candidate


class CorpusPredictionError(ValueError):
    """A model response is not an exact corpus prediction object."""


def parse_corpus_prediction(raw: Mapping[str, Any], case: EvaluationCase) -> Prediction:
    if not isinstance(raw, Mapping) or set(raw) != {"case_id", "decision", "reason_codes"}:
        raise CorpusPredictionError("prediction must contain exactly case_id, decision, reason_codes")
    if raw["case_id"] != case.id:
        raise CorpusPredictionError("prediction case_id does not match the requested case")
    if raw["decision"] not in DECISIONS:
        raise CorpusPredictionError("prediction decision is unsupported")
    codes = raw["reason_codes"]
    if not isinstance(codes, list) or not all(isinstance(code, str) and code for code in codes) or len(set(codes)) != len(codes):
        raise CorpusPredictionError("reason_codes must be unique non-empty strings")
    return Prediction(case.id, raw["decision"], tuple(codes))


@dataclass(frozen=True)
class OllamaCorpusPredictionAdapter:
    config: OllamaGatewayConfig = OllamaGatewayConfig()
    generator: Callable[[OllamaGatewayConfig, str], Mapping[str, Any]] | None = None

    def predict(self, case: EvaluationCase, *, retry_feedback: str | None = None) -> Prediction:
        prompt = (
            "Return only one JSON object with exactly keys case_id, decision, reason_codes. "
            f"case_id must be {case.id!r}; decision must be accept, clarify, or reject. "
            "reason_codes must be an array of unique non-empty snake_case strings. "
            "Use accept only when the request is fully specified, supported by the V1 vocabulary, "
            "and safe; use clarify when required information or intent is ambiguous; use reject when "
            "the request is unsupported, malformed, destructive, or unsafe. Choose reason codes that "
            "name the concrete issue in the case, and do not invent execution capabilities. "
            "Do not include explanations outside the JSON object.\n"
            + ("Previous attempt failed strict schema validation; retry with JSON only.\n" if retry_feedback else "")
            + "Case prompt:\n" + case.prompt
        )
        raw = (self.generator or _ollama_generate)(self.config, prompt)
        return parse_corpus_prediction(raw, case)


def evaluate_corpus_with_ollama(
    corpus: EvaluationCorpus, adapter: OllamaCorpusPredictionAdapter,
) -> CandidateEvaluation:
    """Run every case and return normalized score or a code-only failure."""
    predictions: list[Prediction] = []
    try:
        for case in corpus.cases:
            try:
                predictions.append(adapter.predict(case))
            except CorpusPredictionError:
                # One bounded, schema-only retry may recover a transient
                # formatting failure.  Do not include the original response
                # or gold labels in feedback, and preserve code-only failure
                # semantics if the retry also fails.
                predictions.append(adapter.predict(case, retry_feedback="strict_schema_error"))
    except Exception:
        return evaluate_candidate(corpus, adapter.config.model, failure_code="ollama_prediction_failed")
    return evaluate_candidate(corpus, adapter.config.model, predictions)
