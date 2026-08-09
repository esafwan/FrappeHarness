import pytest

from frappe_harness.operational_budget import ModelBudgetExceeded, ModelUsageLedger, OperationalBudget, enforce_model_usage


def test_default_operational_budget_is_explicit_and_bounded():
    budget = OperationalBudget()
    assert budget.command_timeout_seconds == 300
    assert budget.model_timeout_seconds == 120
    assert budget.max_artifact_bytes == 10_000_000
    assert budget.max_log_bytes == 1_000_000
    assert budget.max_retries == 3
    assert budget.retention_days == 30
    assert budget.max_model_tokens == 8192
    assert budget.max_model_cost_usd == 1.0


@pytest.mark.parametrize("field", ["command_timeout_seconds", "model_timeout_seconds", "max_artifact_bytes", "max_log_bytes", "max_retries", "max_model_tokens", "max_model_cost_usd"])
def test_operational_budget_rejects_non_positive_limits(field):
    with pytest.raises(ValueError):
        OperationalBudget(**{field: 0})


def test_operational_budget_rejects_unbounded_retention():
    with pytest.raises(ValueError, match="retention_days"):
        OperationalBudget(retention_days=31)


def test_model_usage_returns_numeric_cost_without_model_payload():
    cost = enforce_model_usage(OperationalBudget(max_model_cost_usd=1), prompt_tokens=500, completion_tokens=500, prompt_rate_per_1k_usd=0.5, completion_rate_per_1k_usd=1)
    assert cost == 0.75


def test_model_usage_fails_closed_above_cost_cap():
    with pytest.raises(ModelBudgetExceeded, match="cost budget"):
        enforce_model_usage(OperationalBudget(max_model_cost_usd=1), prompt_tokens=1000, completion_tokens=1000, prompt_rate_per_1k_usd=1, completion_rate_per_1k_usd=1)


def test_model_usage_ledger_rejects_cumulative_cost_after_individual_calls_pass():
    ledger = ModelUsageLedger(OperationalBudget(max_model_cost_usd=1))
    first = ledger.record(prompt_tokens=500, completion_tokens=500, prompt_rate_per_1k_usd=0.5, completion_rate_per_1k_usd=1)
    assert first.cost_usd == 0.75
    with pytest.raises(ModelBudgetExceeded, match="cumulative"):
        ledger.record(prompt_tokens=200, completion_tokens=200, prompt_rate_per_1k_usd=1, completion_rate_per_1k_usd=1)
    assert ledger.snapshot == first


def test_model_usage_ledger_does_not_retain_payloads():
    snapshot = ModelUsageLedger(OperationalBudget()).record(
        prompt_tokens=1, completion_tokens=2, prompt_rate_per_1k_usd=0, completion_rate_per_1k_usd=0,
    )
    assert snapshot == type(snapshot)(prompt_tokens=1, completion_tokens=2, cost_usd=0)
