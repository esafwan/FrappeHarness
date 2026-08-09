"""Acquire one normalized, secret-free local Ollama corpus result.

This is the local-provider counterpart to ``live_gemini_corpus_evidence``.  It
retains only deterministic per-case scores and threshold outcomes; model
responses, prompts, and transport details never enter the output artifact.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

from frappe_harness.evaluation_corpus import load_corpus
from frappe_harness.ollama_evaluation import (
    OllamaCorpusPredictionAdapter,
    evaluate_corpus_with_ollama,
)
from frappe_harness.ollama_gateway import OllamaGatewayConfig
from frappe_harness.release_evaluation import CandidateEvaluation


def _normalized(result: CandidateEvaluation) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "model": result.model,
        "corpus_id": result.corpus_id,
        "status": "failed" if result.failure_code else "scored",
        "raw_output_retained": False,
        "unsafe_admission_count": result.unsafe_admission_count,
    }
    if result.failure_code:
        payload["failure_code"] = result.failure_code
        return payload
    assert result.score is not None and result.thresholds is not None
    score = result.score
    payload["score"] = {
        "total_cases": score.total_cases,
        "decision_accuracy": score.decision_accuracy,
        "reason_precision": score.reason_precision,
        "reason_recall": score.reason_recall,
        "unsafe_admission_rate": score.unsafe_admission_rate,
        "missing_case_ids": list(score.missing_case_ids),
        "unknown_case_ids": list(score.unknown_case_ids),
        "case_scores": [
            {
                "case_id": item.case_id,
                "decision_correct": item.decision_correct,
                "reason_precision": item.reason_precision,
                "reason_recall": item.reason_recall,
                "unsafe_admitted": item.unsafe_admitted,
            }
            for item in score.case_scores
        ],
    }
    payload["thresholds"] = {
        "passed": result.thresholds.passed,
        "failures": list(result.thresholds.failures),
    }
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--model")
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        raise SystemExit("output must be a fresh non-symlink path")
    corpus = load_corpus(args.corpus)
    config = OllamaGatewayConfig.from_env()
    if args.model:
        config = replace(config, model=args.model)
    result = evaluate_corpus_with_ollama(corpus, OllamaCorpusPredictionAdapter(config))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(_normalized(result), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "model": result.model,
                "corpus_id": result.corpus_id,
                "status": "failed" if result.failure_code else "scored",
                "output": str(args.output),
                "raw_output_retained": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
