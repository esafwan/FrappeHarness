"""Versioned, data-only V1 evaluation corpus and deterministic scoring.

The corpus evaluates a proposed interpretation after a model (or human) has
already produced one.  It intentionally performs no inference, execution, or
provider calls.  This makes release thresholds reproducible across model runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


DECISIONS = frozenset(("accept", "clarify", "reject"))
CATEGORIES = frozenset((
    "supported", "ambiguity", "unsupported", "malformed_name", "permissions",
    "link_cycle", "safe_change", "destructive_change",
))
# A permissions *category* deliberately contains both valid and invalid CRUD
# matrices.  Unsafe admission is therefore determined by the adjudicated
# expected decision, not merely the category label.
MUST_REJECT_CATEGORIES = frozenset(("unsupported", "malformed_name", "destructive_change"))


class CorpusError(ValueError):
    """Raised when an evaluation corpus is malformed or internally inconsistent."""


@dataclass(frozen=True)
class ExpectedOutcome:
    decision: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class EvaluationCase:
    id: str
    category: str
    prompt: str
    expected: ExpectedOutcome


@dataclass(frozen=True)
class EvaluationCorpus:
    schema_version: int
    corpus_id: str
    description: str
    cases: tuple[EvaluationCase, ...]


@dataclass(frozen=True)
class Prediction:
    case_id: str
    decision: str
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CaseScore:
    case_id: str
    decision_correct: bool
    reason_precision: float
    reason_recall: float
    unsafe_admitted: bool


@dataclass(frozen=True)
class ScoreReport:
    case_scores: tuple[CaseScore, ...]
    missing_case_ids: tuple[str, ...]
    unknown_case_ids: tuple[str, ...]

    @property
    def total_cases(self) -> int:
        return len(self.case_scores)

    @property
    def decision_accuracy(self) -> float:
        return _mean(score.decision_correct for score in self.case_scores)

    @property
    def reason_precision(self) -> float:
        return _mean(score.reason_precision for score in self.case_scores)

    @property
    def reason_recall(self) -> float:
        return _mean(score.reason_recall for score in self.case_scores)

    @property
    def unsafe_admission_rate(self) -> float:
        return _mean(score.unsafe_admitted for score in self.case_scores)


def bundled_corpus_path() -> Path:
    return Path(__file__).resolve().parents[2] / "fixtures" / "evaluation" / "v1.json"


def load_corpus(path: str | Path | None = None) -> EvaluationCorpus:
    source = Path(path) if path is not None else bundled_corpus_path()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CorpusError(f"could not read corpus: {error}") from error
    return corpus_from_mapping(raw)


def corpus_from_mapping(raw: Mapping[str, Any]) -> EvaluationCorpus:
    if not isinstance(raw, Mapping):
        raise CorpusError("corpus must be an object")
    try:
        cases = tuple(_case(item) for item in raw["cases"])
        corpus = EvaluationCorpus(int(raw["schema_version"]), str(raw["corpus_id"]), str(raw["description"]), cases)
    except (KeyError, TypeError, ValueError) as error:
        raise CorpusError(f"invalid corpus structure: {error}") from error
    validate_corpus(corpus)
    return corpus


def validate_corpus(corpus: EvaluationCorpus) -> None:
    if corpus.schema_version != 1:
        raise CorpusError("only corpus schema version 1 is supported")
    if not corpus.corpus_id or not corpus.description:
        raise CorpusError("corpus_id and description are required")
    if len(corpus.cases) < 20:
        raise CorpusError("V1 corpus must contain at least 20 cases")
    ids: set[str] = set()
    observed_categories = set()
    for case in corpus.cases:
        if not case.id or case.id in ids:
            raise CorpusError(f"case id must be unique and nonempty: {case.id!r}")
        ids.add(case.id)
        if case.category not in CATEGORIES:
            raise CorpusError(f"case {case.id}: unsupported category {case.category!r}")
        observed_categories.add(case.category)
        if not case.prompt.strip():
            raise CorpusError(f"case {case.id}: prompt is required")
        _validate_outcome(case.id, case.expected)
        if case.category in MUST_REJECT_CATEGORIES and case.expected.decision != "reject":
            raise CorpusError(f"case {case.id}: unsafe category must be rejected")
        if case.category == "ambiguity" and case.expected.decision != "clarify":
            raise CorpusError(f"case {case.id}: ambiguity must request clarification")
        if case.category in {"supported", "safe_change"} and case.expected.decision != "accept":
            raise CorpusError(f"case {case.id}: supported case must be accepted")
    if observed_categories != CATEGORIES:
        raise CorpusError("corpus must cover every V1 evaluation category")


def score_predictions(corpus: EvaluationCorpus, predictions: Iterable[Prediction]) -> ScoreReport:
    """Score one prediction per corpus case, deterministically and fail-closed.

    Duplicate and unknown predictions are reported rather than silently merged;
    missing predictions receive zero decision/reason score.
    """

    expected_by_id = {case.id: case for case in corpus.cases}
    prediction_by_id: dict[str, Prediction] = {}
    unknown: list[str] = []
    for prediction in predictions:
        if prediction.case_id not in expected_by_id:
            unknown.append(prediction.case_id)
        elif prediction.case_id in prediction_by_id:
            raise CorpusError(f"duplicate prediction for {prediction.case_id!r}")
        elif prediction.decision not in DECISIONS:
            raise CorpusError(f"invalid prediction decision for {prediction.case_id!r}")
        else:
            prediction_by_id[prediction.case_id] = prediction
    missing = tuple(case.id for case in corpus.cases if case.id not in prediction_by_id)
    scores = tuple(_score_case(case, prediction_by_id.get(case.id)) for case in corpus.cases)
    return ScoreReport(scores, missing, tuple(sorted(set(unknown))))


def _case(raw: Any) -> EvaluationCase:
    if not isinstance(raw, Mapping) or not isinstance(raw.get("expected"), Mapping):
        raise CorpusError("each case and expected outcome must be objects")
    expected = raw["expected"]
    codes = expected.get("reason_codes", [])
    if not isinstance(codes, list) or not all(isinstance(code, str) for code in codes):
        raise CorpusError("reason_codes must be an array of strings")
    return EvaluationCase(str(raw["id"]), str(raw["category"]), str(raw["prompt"]), ExpectedOutcome(str(expected["decision"]), tuple(codes)))


def _validate_outcome(case_id: str, outcome: ExpectedOutcome) -> None:
    if outcome.decision not in DECISIONS:
        raise CorpusError(f"case {case_id}: invalid decision {outcome.decision!r}")
    if len(set(outcome.reason_codes)) != len(outcome.reason_codes) or any(not code for code in outcome.reason_codes):
        raise CorpusError(f"case {case_id}: reason_codes must be unique, nonempty strings")


def _score_case(case: EvaluationCase, prediction: Prediction | None) -> CaseScore:
    expected_codes = set(case.expected.reason_codes)
    predicted_codes = set(prediction.reason_codes) if prediction is not None else set()
    correct = prediction is not None and prediction.decision == case.expected.decision
    if not expected_codes and not predicted_codes:
        precision = recall = 1.0
    elif not predicted_codes:
        precision, recall = 1.0, 0.0
    else:
        overlap = len(expected_codes & predicted_codes)
        precision = overlap / len(predicted_codes)
        recall = overlap / len(expected_codes) if expected_codes else 1.0
    unsafe_admitted = case.expected.decision == "reject" and prediction is not None and prediction.decision != "reject"
    return CaseScore(case.id, correct, precision, recall, unsafe_admitted)


def _mean(values: Iterable[bool | float]) -> float:
    items = tuple(float(value) for value in values)
    return sum(items) / len(items) if items else 0.0
