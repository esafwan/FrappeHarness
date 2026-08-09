"""Ordered, run-bound post-migration verification orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .metadata_execution import RunBoundMetadataVerificationResult, RunBoundMetadataVerifier
from .provider_plan import MetadataPlan
from .run_store import RunStore
from .verification_execution import RunBoundBackendVerificationResult, RunBoundVerificationExecutor
from .verification_program import VerificationProbe, VerificationProgram


@dataclass(frozen=True)
class PostMigrationVerificationResult:
    """Metadata evidence followed by optional downstream backend evidence."""

    metadata: RunBoundMetadataVerificationResult
    backend: RunBoundBackendVerificationResult | None


def verify_post_migration(
    store: RunStore,
    run_id: str,
    *,
    plan: MetadataPlan,
    observed_doctypes: Mapping[str, Mapping[str, Any]],
    program: VerificationProgram,
    probe: VerificationProbe,
) -> PostMigrationVerificationResult:
    """Verify metadata first, then run backend checks only after it passes.

    The metadata verifier enforces the exact migration receipt and intent.  A
    mismatch is durable evidence but never admits downstream probes.  An
    interrupted migration therefore cannot be bypassed by this helper; it
    must first be externally reconciled and successfully reapplied.
    """

    metadata = RunBoundMetadataVerifier(store).verify(
        run_id, plan=plan, observed_doctypes=observed_doctypes,
    )
    if metadata.snapshot is None:
        return PostMigrationVerificationResult(metadata, None)
    backend = RunBoundVerificationExecutor(store).execute_backend(run_id, program, probe)
    return PostMigrationVerificationResult(metadata, backend)
