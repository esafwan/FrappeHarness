from __future__ import annotations

import json
import importlib.util
from pathlib import Path

import pytest

from frappe_harness.evaluation_corpus import load_corpus
from frappe_harness.release_evaluation import CandidateEvaluation


def _load_tool():
    path = Path(__file__).parents[1] / "tools" / "live_ollama_corpus_evidence.py"
    spec = importlib.util.spec_from_file_location("live_ollama_corpus_evidence", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ollama_live_tool_normalized_shape_has_no_raw_output(monkeypatch, tmp_path: Path):
    tool = _load_tool()
    corpus = load_corpus()
    expected = CandidateEvaluation(
        "gemma4:latest",
        corpus.corpus_id,
        None,
        None,
        failure_code="ollama_prediction_failed",
    )
    monkeypatch.setattr(tool, "evaluate_corpus_with_ollama", lambda *_: expected)
    monkeypatch.setattr(
        "sys.argv",
        ["live_ollama_corpus_evidence", "--output", str(tmp_path / "result.json")],
    )
    assert tool.main() == 0
    normalized = json.loads((tmp_path / "result.json").read_text())
    assert normalized["raw_output_retained"] is False
    assert "raw_output" not in normalized
    assert normalized["failure_code"] == "ollama_prediction_failed"


def test_ollama_live_tool_rejects_existing_or_symlink_output(monkeypatch, tmp_path: Path):
    tool = _load_tool()
    existing = tmp_path / "existing.json"
    existing.write_text("{}")
    monkeypatch.setattr("sys.argv", ["tool", "--output", str(existing)])
    with pytest.raises(SystemExit, match="fresh non-symlink"):
        tool.main()

    target = tmp_path / "target.json"
    target.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    monkeypatch.setattr("sys.argv", ["tool", "--output", str(link)])
    with pytest.raises(SystemExit, match="fresh non-symlink"):
        tool.main()
