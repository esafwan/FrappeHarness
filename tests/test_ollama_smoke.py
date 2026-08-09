from __future__ import annotations

import os

import pytest

from frappe_harness.model_gateway import ProposalItem, ProposalPayload
from frappe_harness.ollama_gateway import OllamaGatewayConfig, OllamaProposalExtractor, review_with_ollama


pytestmark = pytest.mark.skipif(
    os.environ.get("FRAPPE_HARNESS_OLLAMA_SMOKE") != "1",
    reason="opt-in local Ollama smoke test",
)


def test_gemma4_local_ollama_smoke_round_trip():
    payload = ProposalPayload(
        "task_tracker",
        (ProposalItem("task_status", "The task needs a default status."),),
    )
    review = review_with_ollama(payload, OllamaGatewayConfig.from_env())

    assert review.proposal_id == payload.proposal_id
    assert len(review.findings) == 0
    assert len(review.clarifications) == 1
    assert review.clarifications[0].item_key == "task_status"
    assert "default" in review.clarifications[0].question.lower()


def test_ollama_proposal_extractor_preserves_data_only_boundary(monkeypatch):
    expected = {"name": "task_tracker", "label": "Task Tracker", "module": "task_tracker", "version": 1, "description": "", "roles": [], "entities": [], "screens": []}
    monkeypatch.setattr("frappe_harness.ollama_gateway._ollama_generate", lambda config, prompt: expected)
    assert OllamaProposalExtractor(OllamaGatewayConfig()).extract("make a task tracker") == expected
