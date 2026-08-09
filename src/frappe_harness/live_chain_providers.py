"""Closed Docker providers for generated deployment and clean source recovery."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re

from .artifact_audit import audit_compilation
from .bench_command_policy import InspectedBenchTarget
from .bench_runner import DockerBenchRunner, DockerGeneratedArtifact, RedactedBenchReceipt
from .compiler import CompilationResult, canonical_json
from .recovery_execution import SourceCheckpointReceipt
from .run_store import LockTarget


class GeneratedArtifactDeploymentError(RuntimeError):
    """The compiler output cannot be safely deployed to the exact app."""


class CleanSourceCheckpointError(RuntimeError):
    """The requested checkpoint does not bind the exact inspected app source."""


@dataclass(frozen=True)
class GeneratedArtifactDeploymentReceipt:
    """Secret-free proof of a complete hash-bound artifact deployment."""

    reference: str
    target: LockTarget
    spec_hash: str
    artifact_count: int
    total_bytes: int
    write_receipts: tuple[RedactedBenchReceipt, ...]
    provider_id: str = "docker-generated-artifacts"


class GeneratedArtifactDeploymentProvider:
    """Deploy only audited compiler artifacts to one inspected Docker Bench app."""

    def __init__(self, inspected_target: InspectedBenchTarget, runner: DockerBenchRunner) -> None:
        if not isinstance(inspected_target, InspectedBenchTarget):
            raise ValueError("inspected_target must be an InspectedBenchTarget")
        if not isinstance(runner, DockerBenchRunner):
            raise ValueError("runner must be a DockerBenchRunner")
        self.inspected_target = inspected_target
        self.runner = runner

    def deploy(
        self, *, target: LockTarget, compilation: CompilationResult,
    ) -> GeneratedArtifactDeploymentReceipt:
        expected_target = _lock_target(self.inspected_target)
        if target != expected_target:
            raise GeneratedArtifactDeploymentError("deployment target does not match the inspected app")
        if not isinstance(compilation, CompilationResult):
            raise GeneratedArtifactDeploymentError("deployment requires a CompilationResult")
        report = audit_compilation(compilation)
        if not report.valid:
            codes = ",".join(sorted({issue.code for issue in report.issues}))
            raise GeneratedArtifactDeploymentError(f"compiler artifact audit failed: {codes}")

        app = self.inspected_target.app_slug
        manifest_path = f"{app}/harness/ownership-manifest.json"
        manifest = compilation.ownership_manifest
        if manifest.get("app_slug") != app or manifest.get("spec_hash") != compilation.spec_hash:
            raise GeneratedArtifactDeploymentError("ownership manifest does not bind the inspected app and spec")
        entries = manifest.get("owned_files")
        if not isinstance(entries, list):
            raise GeneratedArtifactDeploymentError("ownership manifest does not contain typed entries")
        owned = {
            entry["path"]: entry["sha256"]
            for entry in entries
            if isinstance(entry, dict)
            and isinstance(entry.get("path"), str)
            and isinstance(entry.get("sha256"), str)
        }
        if len(owned) != len(entries) or set(owned) != set(compilation.artifacts) - {manifest_path}:
            raise GeneratedArtifactDeploymentError("ownership manifest membership is not exact")
        for path, digest in owned.items():
            content = compilation.artifacts[path]
            if sha256(content).hexdigest() != digest:
                raise GeneratedArtifactDeploymentError("ownership manifest digest does not match artifact bytes")

        total_bytes = sum(len(content) for content in compilation.artifacts.values())
        if self.runner.budget and total_bytes > self.runner.budget.max_artifact_bytes:
            raise GeneratedArtifactDeploymentError("deployment exceeds the configured artifact byte budget")
        # The ownership manifest is the completion marker and is written last.
        paths = sorted(set(compilation.artifacts) - {manifest_path}) + [manifest_path]
        receipts: list[RedactedBenchReceipt] = []
        bundle_rows: list[dict[str, str]] = []
        for path in paths:
            content = compilation.artifacts[path]
            digest = sha256(content).hexdigest()
            receipt = self.runner.write_generated_artifact(
                DockerGeneratedArtifact(path, content, digest),
                inspected_target=self.inspected_target,
            )
            if receipt.exit_code != 0:
                raise GeneratedArtifactDeploymentError("generated artifact write did not complete successfully")
            receipts.append(receipt)
            bundle_rows.append({"path": path, "sha256": digest})
        bundle_hash = sha256(canonical_json(bundle_rows)).hexdigest()
        return GeneratedArtifactDeploymentReceipt(
            f"docker-generated-artifacts:{bundle_hash}",
            target,
            compilation.spec_hash,
            len(paths),
            total_bytes,
            tuple(receipts),
        )


class CleanSourceCheckpointProvider:
    """Produce a checkpoint only from a clean exact-app Git HEAD in Docker."""

    def __init__(self, inspected_target: InspectedBenchTarget, runner: DockerBenchRunner) -> None:
        if not isinstance(inspected_target, InspectedBenchTarget):
            raise ValueError("inspected_target must be an InspectedBenchTarget")
        if not isinstance(runner, DockerBenchRunner):
            raise ValueError("runner must be a DockerBenchRunner")
        self.inspected_target = inspected_target
        self.runner = runner

    def create(self, *, target: LockTarget, spec_hash: str, plan_hash: str) -> SourceCheckpointReceipt:
        if target != _lock_target(self.inspected_target):
            raise CleanSourceCheckpointError("checkpoint target does not match the inspected app")
        for label, digest in (("spec_hash", spec_hash), ("plan_hash", plan_hash)):
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise CleanSourceCheckpointError(f"{label} must be a lower-case SHA-256 digest")
        inspection = self.runner.inspect_clean_source(inspected_target=self.inspected_target)
        return SourceCheckpointReceipt(
            f"git:{inspection.checkpoint}",
            inspection.checkpoint,
            target,
            spec_hash,
            plan_hash,
            provider_id="docker-git-clean",
        )


def _lock_target(target: InspectedBenchTarget) -> LockTarget:
    return LockTarget(target.bench_path, target.site_name, target.app_slug)
