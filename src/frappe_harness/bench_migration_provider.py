"""Typed Frappe-native migration provider backed by the closed Bench runner."""

from __future__ import annotations

from hashlib import sha256

from .bench_command_policy import InspectedBenchTarget, SiteMigrate
from .bench_runner import BenchRunner, DockerBenchRunner, DockerContainerTarget
from .compiler import CompilationResult
from .migration_execution import MigrationProviderReceipt
from .operational_budget import OperationalBudget
from .provider_plan import MetadataPlan
from .run_store import LockTarget


class BenchMigrationProvider:
    """Apply only ``SiteMigrate`` for the inspected target supplied at construction."""

    provider_id = "frappe-native"

    def __init__(
        self, inspected_target: InspectedBenchTarget, *, runner: BenchRunner | None = None,
        container: DockerContainerTarget | None = None, budget: OperationalBudget | None = None,
    ) -> None:
        self.inspected_target = inspected_target
        self._runner_was_injected = runner is not None
        if container is not None and not isinstance(container, DockerContainerTarget):
            raise ValueError("container must be a DockerContainerTarget")
        self.container = container
        if runner is not None and budget is not None:
            raise ValueError("provide runner or budget, not both")
        if runner is not None and container is not None:
            raise ValueError("provide runner or container, not both")
        self.runner = runner or (
            DockerBenchRunner(container, budget=budget) if container is not None else BenchRunner(budget=budget)
        )

    def bind_budget(self, budget: OperationalBudget | None) -> "BenchMigrationProvider":
        """Return a provider bound to a run's immutable budget.

        The migration executor uses this optional capability when present. A
        caller-supplied runner remains authoritative for test/double
        providers and is never silently replaced.
        """
        if budget is None or self._runner_was_injected:
            return self
        return BenchMigrationProvider(self.inspected_target, container=self.container, budget=budget)

    def apply(
        self, *, target: LockTarget, plan: MetadataPlan, compilation: CompilationResult,
    ) -> MigrationProviderReceipt:
        if target != LockTarget(
            self.inspected_target.bench_path,
            self.inspected_target.site_name,
            self.inspected_target.app_slug,
        ):
            raise ValueError("inspected Bench target does not match the run target")
        if plan.provider != self.provider_id or compilation.spec_hash != plan.spec_hash:
            raise ValueError("migration inputs do not bind the approved Frappe-native plan")
        execution = self.runner.run(SiteMigrate(self.inspected_target), inspected_target=self.inspected_target)
        process_receipt = execution.receipt
        reference = "bench:migrate:" + sha256("\0".join(process_receipt.argv).encode("utf-8")).hexdigest()
        return MigrationProviderReceipt(
            reference,
            process_receipt.exit_code,
            process_receipt.stdout_bytes,
            process_receipt.stdout_sha256,
            process_receipt.stderr_bytes,
            process_receipt.stderr_sha256,
        )
