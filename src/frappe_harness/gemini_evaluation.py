"""Provider-neutral Gemini corpus adapter with the same strict data boundary as Ollama."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .evaluation_corpus import EvaluationCase, EvaluationCorpus, Prediction
from .ollama_evaluation import CorpusPredictionError, parse_corpus_prediction
from .release_evaluation import CandidateEvaluation, evaluate_candidate


@dataclass(frozen=True)
class GeminiCorpusConfig:
    model: str = "gemini-3.5-flash-lite"
    api_key: str | None = None
    timeout_seconds: int = 120
    max_output_tokens: int = 512

    @classmethod
    def from_env(cls) -> "GeminiCorpusConfig":
        return cls(
            model=os.environ.get("FRAPPE_HARNESS_GEMINI_MODEL", cls.model),
            api_key=os.environ.get("GEMINI_API_KEY"),
        )


class GeminiCorpusError(RuntimeError):
    """Gemini transport or response-shape failure."""


@dataclass(frozen=True)
class GeminiCorpusPredictionAdapter:
    config: GeminiCorpusConfig = GeminiCorpusConfig()
    generator: Callable[[GeminiCorpusConfig, str], Mapping[str, Any]] | None = None

    def predict(self, case: EvaluationCase, *, retry_feedback: str | None = None) -> Prediction:
        prompt = (
            "Return only one JSON object with exactly keys case_id, decision, reason_codes. "
            f"case_id must be {case.id!r}; decision must be accept, clarify, or reject. "
            "reason_codes must be an array of unique non-empty snake_case strings. "
            "Use accept only when fully specified, supported, and safe; clarify for ambiguity; "
            "reject for unsupported, malformed, destructive, or unsafe requests.\n"
            + ("Previous attempt failed strict schema validation; retry JSON only.\n" if retry_feedback else "")
            + "Case prompt:\n" + case.prompt
        )
        raw = (self.generator or _gemini_generate)(self.config, prompt)
        try:
            return parse_corpus_prediction(raw, case)
        except CorpusPredictionError:
            raise
        except Exception as error:
            raise CorpusPredictionError("Gemini prediction was not a valid corpus object") from error


def evaluate_corpus_with_gemini(corpus: EvaluationCorpus, adapter: GeminiCorpusPredictionAdapter) -> CandidateEvaluation:
    predictions: list[Prediction] = []
    try:
        for case in corpus.cases:
            try:
                predictions.append(adapter.predict(case))
            except CorpusPredictionError:
                predictions.append(adapter.predict(case, retry_feedback="strict_schema_error"))
    except Exception:
        return evaluate_candidate(corpus, adapter.config.model, failure_code="gemini_prediction_failed")
    return evaluate_candidate(corpus, adapter.config.model, predictions)


def _gemini_generate(config: GeminiCorpusConfig, prompt: str) -> Mapping[str, Any]:
    if not config.api_key:
        raise GeminiCorpusError("GEMINI_API_KEY is required")
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{quote(config.model, safe='')}:generateContent?key={quote(config.api_key, safe='')}"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": config.max_output_tokens, "responseMimeType": "application/json"},
    }).encode()
    try:
        with urlopen(Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST"), timeout=config.timeout_seconds) as response:
            payload = json.loads(response.read().decode())
    except (URLError, OSError, json.JSONDecodeError) as error:
        raise GeminiCorpusError("Gemini request failed") from error
    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
        result = json.loads(text)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise GeminiCorpusError("Gemini response did not contain one JSON object") from error
    if not isinstance(result, Mapping):
        raise GeminiCorpusError("Gemini response JSON must be an object")
    return result
