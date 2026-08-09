"""Run-bound typed build/cache/test/browser evidence for the golden app."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .bench_command_policy import BuildApp, ClearSiteCache, InspectedBenchTarget, RunAppTests
from .bench_execution import RunBoundBenchExecutor
from .run_store import CommandReceipt, LifecycleEvidenceRecord, LockTarget, RunStore


class GoldenOperationsError(RuntimeError):
    """A typed golden-operation sequence could not produce complete evidence."""


@dataclass(frozen=True)
class GoldenOperationsResult:
    command_receipts: tuple[CommandReceipt, ...]
    evidence: LifecycleEvidenceRecord
    browser_evidence: LifecycleEvidenceRecord


class RunBoundGoldenOperationsExecutor:
    """Persist one exact-target sequence without accepting shell text."""

    def __init__(self, store: RunStore, *, bench: RunBoundBenchExecutor | None = None) -> None:
        self.store = store
        self.bench = bench or RunBoundBenchExecutor(store)

    def execute(
        self,
        run_id: str,
        *,
        inspected_target: InspectedBenchTarget,
        browser_probe: Callable[[], bool],
    ) -> GoldenOperationsResult:
        run = self.store.get_run(run_id)
        target = LockTarget(inspected_target.bench_path, inspected_target.site_name, inspected_target.app_slug)
        if run.target != target:
            raise GoldenOperationsError("golden operation target does not match the run intent")
        if not callable(browser_probe):
            raise GoldenOperationsError("browser_probe must be a callable read-only acceptance probe")
        receipts: list[CommandReceipt] = []
        for operation in (BuildApp(inspected_target), ClearSiteCache(inspected_target), RunAppTests(inspected_target)):
            result = self.bench.execute(run_id, operation, inspected_target=inspected_target)
            if result.command_receipt.status != "completed" or result.command_receipt.exit_code != 0:
                raise GoldenOperationsError(f"typed {type(operation).__name__} did not complete successfully")
            receipts.append(result.command_receipt)
        evidence = self.store.record_lifecycle_evidence(run_id, "golden_operations", {
            "version": "v1",
            "target": {"bench": target.bench, "site": target.site, "app": target.app},
            "operations": [
                {"operation": type(operation).__name__, "command_receipt_id": receipt.id}
                for operation, receipt in zip(
                    (BuildApp(inspected_target), ClearSiteCache(inspected_target), RunAppTests(inspected_target)), receipts
                )
            ],
        })
        try:
            passed = browser_probe()
        except Exception as error:
            raise GoldenOperationsError("browser acceptance probe failed") from error
        if passed is not True:
            raise GoldenOperationsError("browser acceptance probe did not pass")
        browser_evidence = self.store.record_lifecycle_evidence(run_id, "browser_acceptance", {
            "version": "v1", "target": {"bench": target.bench, "site": target.site, "app": target.app},
            "mutation_attempted": False,
        })
        return GoldenOperationsResult(tuple(receipts), evidence, browser_evidence)
