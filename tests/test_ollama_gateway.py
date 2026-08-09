from __future__ import annotations

import json
import pytest

from frappe_harness.ollama_gateway import OllamaGatewayConfig, OllamaGatewayError, _ollama_generate
from frappe_harness.operational_budget import ModelUsageLedger, OperationalBudget


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"response":"{}"}'


def test_ollama_generation_uses_deterministic_seed(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr("frappe_harness.ollama_gateway.urlopen", fake_urlopen)
    _ollama_generate(OllamaGatewayConfig(model="gemma4:latest", timeout_seconds=17), "prompt")
    assert captured["body"]["options"] == {"temperature": 0, "seed": 0, "num_predict": 8192}
    assert captured["timeout"] == 17


def test_ollama_generation_is_capped_by_model_budget(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr("frappe_harness.ollama_gateway.urlopen", fake_urlopen)
    budget = OperationalBudget(model_timeout_seconds=9, max_model_tokens=256)
    _ollama_generate(OllamaGatewayConfig(timeout_seconds=17, budget=budget), "prompt")
    assert captured["timeout"] == 9
    assert captured["body"]["options"]["num_predict"] == 256


def test_ollama_generation_records_real_usage_only_with_caller_pricing(monkeypatch):
    class UsageResponse(_Response):
        def read(self):
            return b'{"response":"{}", "prompt_eval_count":10, "eval_count":20}'

    monkeypatch.setattr("frappe_harness.ollama_gateway.urlopen", lambda request, timeout: UsageResponse())
    ledger = ModelUsageLedger(OperationalBudget(max_model_cost_usd=1))
    config = OllamaGatewayConfig(
        usage_ledger=ledger, prompt_rate_per_1k_usd=1, completion_rate_per_1k_usd=2,
    )
    _ollama_generate(config, "prompt")
    assert ledger.snapshot.prompt_tokens == 10
    assert ledger.snapshot.completion_tokens == 20
    assert ledger.snapshot.cost_usd == 0.05


def test_ollama_generation_with_ledger_rejects_missing_usage_counters(monkeypatch):
    monkeypatch.setattr("frappe_harness.ollama_gateway.urlopen", lambda request, timeout: _Response())
    ledger = ModelUsageLedger(OperationalBudget())
    config = OllamaGatewayConfig(usage_ledger=ledger, prompt_rate_per_1k_usd=1, completion_rate_per_1k_usd=1)
    with pytest.raises(OllamaGatewayError, match="usage counters"):
        _ollama_generate(config, "prompt")
    assert ledger.snapshot.prompt_tokens == 0


def test_ollama_usage_ledger_must_share_gateway_budget():
    ledger = ModelUsageLedger(OperationalBudget(max_model_cost_usd=2))
    with pytest.raises(ValueError, match="budget envelope"):
        OllamaGatewayConfig(
            budget=OperationalBudget(max_model_cost_usd=1), usage_ledger=ledger,
            prompt_rate_per_1k_usd=1, completion_rate_per_1k_usd=1,
        )
