"""Read-only inspection bridge for run-bound migration reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .run_store import LockTarget, RunStore
from .environment_preflight import UnsupportedEnvironmentFact
from .runtime_lifecycle import (
    MigrationReconciliationReceipt,
    MigrationReconciliationResult,
    RunLifecycleCoordinator,
)


@dataclass(frozen=True)
class MigrationInspectionFacts:
    """Secret-free facts returned by an external read-only inspection."""

    reference: str
    target: LockTarget
    spec_hash: str
    plan_hash: str
    outcome: str = "inspected"


def parse_migration_inspection_facts(raw: object) -> MigrationInspectionFacts:
    """Parse a strict, secret-free read-only inspection payload."""
    if not isinstance(raw, Mapping):
        raise ValueError("migration inspection facts must be an object")
    allowed = {"reference", "target", "spec_hash", "plan_hash", "outcome"}
    if set(raw) - allowed:
        raise ValueError("migration inspection facts contain unsupported fields")
    target = raw.get("target")
    if not isinstance(target, Mapping) or set(target) != {"bench", "site", "app"}:
        raise ValueError("migration inspection target must contain exactly bench/site/app")
    values = (raw.get("reference"), raw.get("spec_hash"), raw.get("plan_hash"), raw.get("outcome", "inspected"), target.get("bench"), target.get("site"), target.get("app"))
    if any(not isinstance(value, str) for value in values):
        raise ValueError("migration inspection facts have invalid types")
    try:
        typed_target = LockTarget(target["bench"], target["site"], target["app"])
        return MigrationInspectionFacts(
            raw["reference"], typed_target, raw["spec_hash"],
            raw["plan_hash"], raw.get("outcome", "inspected"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("migration inspection facts have invalid types or values") from error


class MigrationInspectionProbe(Protocol):
    def inspect(self, target: LockTarget) -> MigrationInspectionFacts:
        """Inspect mutation state without invoking a schema-changing operation."""


class RunnerBackedMigrationInspectionProbe:
    """Adapt one explicitly read-only runner without exposing raw commands."""

    def __init__(self, runner: Callable[[LockTarget], object]) -> None:
        self.runner = runner

    def inspect(self, target: LockTarget) -> MigrationInspectionFacts:
        try:
            payload = self.runner(target)
        except (FileNotFoundError, NotImplementedError) as error:
            raise UnsupportedEnvironmentFact("no read-only migration inspection runner is available") from error
        return parse_migration_inspection_facts(payload)


def reconcile_from_inspection(
    store: RunStore, run_id: str, probe: MigrationInspectionProbe,
) -> MigrationReconciliationResult:
    """Convert read-only facts into exact reconciliation evidence and reset retry state."""
    run = store.get_run(run_id)
    facts = probe.inspect(run.target)
    receipt = MigrationReconciliationReceipt(
        facts.reference, facts.target, facts.spec_hash, facts.plan_hash, facts.outcome,
    )
    return RunLifecycleCoordinator(store).reconcile_migration(run_id, receipt)
