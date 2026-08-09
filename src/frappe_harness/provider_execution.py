"""Narrow bridges from trusted providers to run-bound lifecycle executors."""

from __future__ import annotations

from typing import Mapping
import os

from .frappe_verification_probe import CreatedRecord, FrappeVerificationProbe, FrappeVerificationProvider, VerificationFixtures
from .frappectl_adapter import FrappectlReadOnlyAdapter
from .frappectl_policy import DoctypeShowRequest
from .frappectl_preflight import normalize_doctype_show_payload
from .metadata_execution import RunBoundMetadataVerificationResult, RunBoundMetadataVerifier
from .provider_plan import MetadataPlan
from .run_store import RunState, RunStore
from .verification_execution import RunBoundBackendVerificationResult, RunBoundVerificationExecutor
from .verification_program import VerificationProgram


class ProviderExecutionAdmissionError(RuntimeError):
    """A provider adapter cannot be safely bound to the requested run."""


class RunBoundFrappectlMetadataAdapter:
    """Collect only planned DocType metadata through the read-only policy."""

    def __init__(self, store: RunStore, *, profile: str | None, adapter: FrappectlReadOnlyAdapter) -> None:
        self.store = store
        self.profile = profile
        self.adapter = adapter

    def verify(self, run_id: str, *, plan: MetadataPlan) -> RunBoundMetadataVerificationResult:
        run = self.store.get_run(run_id)
        if run.state is not RunState.RUNNING:
            raise ProviderExecutionAdmissionError("provider verification requires a running approved run")
        if self.profile is None:
            # Headless disposable/CI runs may use frappectl's documented
            # process-environment credential path. Bind that environment to
            # the immutable run site before allowing any read.
            bound_site = os.environ.get("FRAPPE_HARNESS_SITE_NAME", os.environ.get("FRAPPE_SITE"))
            if bound_site != run.target.site:
                raise ProviderExecutionAdmissionError("frappectl environment site does not match the run's approved site")
        elif self.profile != run.target.site:
            raise ProviderExecutionAdmissionError("frappectl profile does not match the run's approved site")
        observed: dict[str, Mapping[str, object]] = {}
        receipt_evidence_ids: list[str] = []
        for operation in plan.operations:
            # ``frappectl doctype show`` omits semantic defaults in its
            # human-oriented projection; raw mode is still read-only and is
            # required for exact provider-plan equivalence.
            result = self.adapter.inspect(DoctypeShowRequest(operation.doctype, raw=True))
            receipt_evidence = self.store.record_lifecycle_evidence(run_id, "frappectl_receipt", {
                "doctype": operation.doctype,
                "argv": list(result.receipt.argv),
                "exit_code": result.receipt.exit_code,
                "stdout_bytes": result.receipt.stdout_bytes,
                "stdout_sha256": result.receipt.stdout_sha256,
                "stderr_bytes": result.receipt.stderr_bytes,
                "stderr_sha256": result.receipt.stderr_sha256,
            })
            receipt_evidence_ids.append(receipt_evidence.id)
            if result.receipt.exit_code != 0 or result.payload is None:
                raise ProviderExecutionAdmissionError("frappectl metadata observation failed")
            try:
                observed[operation.doctype] = normalize_doctype_show_payload(
                    result.payload, expected_name=operation.doctype,
                )
            except ValueError as error:
                raise ProviderExecutionAdmissionError("frappectl metadata payload was invalid") from error
        return RunBoundMetadataVerifier(self.store).verify(
            run_id, plan=plan, observed_doctypes=observed,
            provider_receipt_evidence_ids=tuple(receipt_evidence_ids),
        )


class RunBoundFrappeVerificationAdapter:
    """Bind a fixed actor/provider map to the generated verification program."""

    def __init__(
        self,
        store: RunStore,
        *,
        actors: Mapping[str, FrappeVerificationProvider],
        fixtures: VerificationFixtures,
    ) -> None:
        self.store = store
        self.actors = dict(actors)
        self.fixtures = fixtures
        self._probe: FrappeVerificationProbe | None = None

    @property
    def cleanup_records(self) -> tuple[CreatedRecord, ...]:
        """Names created by the last execution; empty before any execution."""

        return self._probe.cleanup_records if self._probe is not None else ()

    def execute_backend(
        self, run_id: str, *, program: VerificationProgram,
    ) -> RunBoundBackendVerificationResult:
        run = self.store.get_run(run_id)
        if run.state is not RunState.RUNNING:
            raise ProviderExecutionAdmissionError("provider verification requires a running approved run")
        if not self.actors:
            raise ProviderExecutionAdmissionError("at least one typed verification actor is required")
        self._probe = FrappeVerificationProbe(self.actors, self.fixtures)
        return RunBoundVerificationExecutor(self.store).execute_backend(run_id, program, self._probe)
