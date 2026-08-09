"""Fail-closed operational limits for command, model, and evidence work."""

from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class OperationalBudget:
    """Bound one run's resource and retention envelope before execution."""

    command_timeout_seconds: float = 300.0
    model_timeout_seconds: float = 120.0
    max_artifact_bytes: int = 10_000_000
    max_log_bytes: int = 1_000_000
    max_retries: int = 3
    retention_days: int = 30
    max_model_tokens: int = 8_192
    max_model_cost_usd: float = 1.0

    def __post_init__(self) -> None:
        for name in ("command_timeout_seconds", "model_timeout_seconds", "max_model_cost_usd"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")
        for name in ("max_artifact_bytes", "max_log_bytes", "max_retries", "max_model_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if isinstance(self.retention_days, bool) or not isinstance(self.retention_days, int) or not 0 <= self.retention_days <= 30:
            raise ValueError("retention_days must be between 0 and 30")


class ModelBudgetExceeded(ValueError):
    """Raised when caller-supplied numeric model usage exceeds the budget."""


@dataclass(frozen=True)
class ModelUsageSnapshot:
    """Secret-free cumulative model usage for one bounded run."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


class ModelUsageLedger:
    """Accumulate caller-supplied numeric usage without retaining model output."""

    def __init__(self, budget: OperationalBudget) -> None:
        if not isinstance(budget, OperationalBudget):
            raise ValueError("budget must be an OperationalBudget")
        self.budget = budget
        self._snapshot = ModelUsageSnapshot()

    @property
    def snapshot(self) -> ModelUsageSnapshot:
        return self._snapshot

    def record(
        self, *, prompt_tokens: int, completion_tokens: int,
        prompt_rate_per_1k_usd: float, completion_rate_per_1k_usd: float,
    ) -> ModelUsageSnapshot:
        """Commit one usage event only if cumulative cost stays within budget."""
        cost = enforce_model_usage(
            self.budget,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            prompt_rate_per_1k_usd=prompt_rate_per_1k_usd,
            completion_rate_per_1k_usd=completion_rate_per_1k_usd,
        )
        total = self._snapshot.cost_usd + cost
        if total > self.budget.max_model_cost_usd:
            raise ModelBudgetExceeded("cumulative model usage exceeds the configured cost budget")
        self._snapshot = ModelUsageSnapshot(
            self._snapshot.prompt_tokens + prompt_tokens,
            self._snapshot.completion_tokens + completion_tokens,
            total,
        )
        return self._snapshot


def enforce_model_usage(budget: OperationalBudget, *, prompt_tokens: int, completion_tokens: int, prompt_rate_per_1k_usd: float, completion_rate_per_1k_usd: float) -> float:
    """Return bounded numeric cost, or fail closed at the configured cap."""
    if not isinstance(budget, OperationalBudget):
        raise ValueError("budget must be an OperationalBudget")
    for name, value in (("prompt_tokens", prompt_tokens), ("completion_tokens", completion_tokens)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    for name, value in (("prompt_rate_per_1k_usd", prompt_rate_per_1k_usd), ("completion_rate_per_1k_usd", completion_rate_per_1k_usd)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be a non-negative finite number")
    cost = (prompt_tokens * prompt_rate_per_1k_usd + completion_tokens * completion_rate_per_1k_usd) / 1000
    if not math.isfinite(cost) or cost > budget.max_model_cost_usd:
        raise ModelBudgetExceeded("model usage exceeds the configured cost budget")
    return cost
