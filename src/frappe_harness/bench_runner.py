"""Bounded subprocess adapter for typed, policy-approved Bench operations.

The runner deliberately has no raw argv or cwd entry point.  A caller can
only execute an operation represented by ``bench_command_policy`` and bound to
the same immutable target that was inspected before admission.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import math
from pathlib import PurePosixPath
import re
import subprocess

from .bench_command_policy import BenchOperation, InspectedBenchTarget, build_bench_argv
from .operational_budget import OperationalBudget


class BenchToolUnavailable(RuntimeError):
    """Raised when the policy-approved Bench execution tool is unavailable."""


class BenchCommandTimedOut(RuntimeError):
    """Raised when a Bench operation exceeds the configured execution bound."""


class BenchOutputTooLarge(RuntimeError):
    """Raised when command output exceeds the configured evidence budget."""


class DockerContainerPolicyError(ValueError):
    """Raised when a Docker container identifier is outside the closed policy."""


class DockerArtifactPolicyError(ValueError):
    """Raised when a generated artifact is outside the closed Docker policy."""


class DockerSourceCheckpointError(RuntimeError):
    """Raised when the exact app repository is absent, dirty, or unidentifiable."""


_CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True)
class DockerContainerTarget:
    """One explicitly configured Docker container admitted for Bench execution."""

    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _CONTAINER_NAME_RE.fullmatch(self.name):
            raise DockerContainerPolicyError("container name must be an allowlisted identifier")


@dataclass(frozen=True)
class DockerGeneratedArtifact:
    """One hash-bound compiler artifact admitted to the Docker write seam."""

    path: str
    content: bytes
    sha256: str

    def __post_init__(self) -> None:
        candidate = PurePosixPath(self.path) if isinstance(self.path, str) else None
        if (
            candidate is None
            or candidate.is_absolute()
            or candidate.as_posix() != self.path
            or ".." in candidate.parts
            or len(candidate.parts) < 2
        ):
            raise DockerArtifactPolicyError("artifact path must be a normalized relative app path")
        if not isinstance(self.content, bytes):
            raise DockerArtifactPolicyError("artifact content must be bytes")
        if not isinstance(self.sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise DockerArtifactPolicyError("artifact sha256 must be a lower-case digest")
        if hashlib.sha256(self.content).hexdigest() != self.sha256:
            raise DockerArtifactPolicyError("artifact bytes do not match the admitted digest")


@dataclass(frozen=True)
class RedactedBenchReceipt:
    """Process evidence without persisting potentially sensitive child output."""

    argv: tuple[str, ...]
    exit_code: int
    started_at: str
    completed_at: str
    stdout_bytes: int
    stdout_sha256: str
    stderr_bytes: int
    stderr_sha256: str


@dataclass(frozen=True)
class BenchExecutionResult:
    """The exit status and redacted evidence for one allowed Bench operation."""

    receipt: RedactedBenchReceipt


@dataclass(frozen=True)
class DockerCleanSourceInspection:
    """Normalized clean-repository evidence with child output discarded."""

    checkpoint: str
    status_receipt: RedactedBenchReceipt
    head_receipt: RedactedBenchReceipt


class BenchRunner:
    """Execute only an exact policy argv in the inspected Bench directory."""

    def __init__(self, *, timeout_seconds: float = 300.0, budget: OperationalBudget | None = None) -> None:
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool):
            raise ValueError("timeout_seconds must be a positive finite number")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive finite number")
        self.timeout_seconds = float(timeout_seconds)
        if budget is not None and not isinstance(budget, OperationalBudget):
            raise ValueError("budget must be an OperationalBudget")
        self.budget = budget

    def run(self, operation: BenchOperation, *, inspected_target: InspectedBenchTarget) -> BenchExecutionResult:
        """Run one typed allowlisted operation and return no child-process output."""

        bench_argv = build_bench_argv(operation, inspected_target=inspected_target)
        argv, cwd = self._subprocess_invocation(bench_argv, inspected_target=inspected_target)
        timeout = min(
            self.timeout_seconds,
            self.budget.command_timeout_seconds if self.budget else self.timeout_seconds,
        )
        started_at = _now()
        try:
            completed = subprocess.run(
                argv,
                shell=False,
                check=False,
                cwd=cwd,
                capture_output=True,
                text=False,
                timeout=timeout,
            )
        except FileNotFoundError as error:
            raise BenchToolUnavailable(self._tool_unavailable_message()) from error
        except subprocess.TimeoutExpired as error:
            raise BenchCommandTimedOut(
                f"approved Bench operation exceeded the {timeout:g}-second execution limit"
            ) from error

        completed_at = _now()
        stdout = completed.stdout or b""
        stderr = completed.stderr or b""
        if self.budget and (
            len(stdout) > self.budget.max_log_bytes
            or len(stderr) > self.budget.max_log_bytes
            or len(stdout) + len(stderr) > self.budget.max_artifact_bytes
        ):
            raise BenchOutputTooLarge("Bench output exceeds the configured log/artifact byte budget")
        return BenchExecutionResult(
            receipt=RedactedBenchReceipt(
                argv=argv,
                exit_code=completed.returncode,
                started_at=started_at,
                completed_at=completed_at,
                stdout_bytes=len(stdout),
                stdout_sha256=hashlib.sha256(stdout).hexdigest(),
                stderr_bytes=len(stderr),
                stderr_sha256=hashlib.sha256(stderr).hexdigest(),
            )
        )

    def _subprocess_invocation(
        self,
        bench_argv: tuple[str, ...],
        *,
        inspected_target: InspectedBenchTarget,
    ) -> tuple[tuple[str, ...], str | None]:
        return bench_argv, inspected_target.bench_path

    def _tool_unavailable_message(self) -> str:
        return "bench is unavailable; install the pinned Bench runtime before executing an approved operation"


class DockerBenchRunner(BenchRunner):
    """Run the closed Bench allowlist inside one configured Docker container.

    Docker is only a transport: callers still supply a typed ``BenchOperation``
    and its exact immutable inspection target.  No raw Docker argv, shell text,
    executable override, or additional flag is accepted.
    """

    def __init__(
        self,
        container: DockerContainerTarget,
        *,
        timeout_seconds: float = 300.0,
        budget: OperationalBudget | None = None,
    ) -> None:
        if not isinstance(container, DockerContainerTarget):
            raise DockerContainerPolicyError("container must be a DockerContainerTarget")
        self.container = container
        super().__init__(timeout_seconds=timeout_seconds, budget=budget)

    def _subprocess_invocation(
        self,
        bench_argv: tuple[str, ...],
        *,
        inspected_target: InspectedBenchTarget,
    ) -> tuple[tuple[str, ...], None]:
        return (
            "docker",
            "exec",
            "--workdir",
            inspected_target.bench_path,
            self.container.name,
            *bench_argv,
        ), None

    def _tool_unavailable_message(self) -> str:
        return "docker is unavailable; install the pinned Docker CLI before executing an approved operation"

    def write_generated_artifact(
        self,
        artifact: DockerGeneratedArtifact,
        *,
        inspected_target: InspectedBenchTarget,
    ) -> RedactedBenchReceipt:
        """Atomically write one hash-bound artifact below ``apps/<exact-app>``."""

        if not isinstance(artifact, DockerGeneratedArtifact):
            raise DockerArtifactPolicyError("artifact must be a DockerGeneratedArtifact")
        if artifact.path.split("/", 1)[0] != inspected_target.app_slug:
            raise DockerArtifactPolicyError("artifact path does not belong to the inspected app")
        if self.budget and len(artifact.content) > self.budget.max_artifact_bytes:
            raise DockerArtifactPolicyError("artifact exceeds the configured artifact byte budget")
        receipt, _stdout = self._run_fixed_docker(
            (
                "python3", "-c", _WRITE_GENERATED_ARTIFACT,
                artifact.path, artifact.sha256, inspected_target.app_slug,
            ),
            inspected_target=inspected_target,
            workdir=inspected_target.bench_path,
            input_bytes=artifact.content,
        )
        return receipt

    def inspect_clean_source(
        self, *, inspected_target: InspectedBenchTarget,
    ) -> DockerCleanSourceInspection:
        """Prove the exact app Git repository is clean and return its immutable HEAD."""

        app_workdir = f"{inspected_target.bench_path}/apps/{inspected_target.app_slug}"
        status_receipt, status = self._run_fixed_docker(
            ("git", "status", "--porcelain=v1", "--untracked-files=all"),
            inspected_target=inspected_target,
            workdir=app_workdir,
        )
        if status_receipt.exit_code != 0:
            raise DockerSourceCheckpointError("clean source status inspection failed")
        if status:
            raise DockerSourceCheckpointError("source checkpoint requires a clean app repository")
        head_receipt, head_output = self._run_fixed_docker(
            ("git", "rev-parse", "--verify", "HEAD"),
            inspected_target=inspected_target,
            workdir=app_workdir,
        )
        if head_receipt.exit_code != 0:
            raise DockerSourceCheckpointError("source checkpoint HEAD inspection failed")
        try:
            checkpoint = head_output.decode("ascii").strip()
        except UnicodeDecodeError as error:
            raise DockerSourceCheckpointError("source checkpoint HEAD is not an object digest") from error
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", checkpoint):
            raise DockerSourceCheckpointError("source checkpoint HEAD is not an object digest")
        return DockerCleanSourceInspection(checkpoint, status_receipt, head_receipt)

    def _run_fixed_docker(
        self,
        command: tuple[str, ...],
        *,
        inspected_target: InspectedBenchTarget,
        workdir: str,
        input_bytes: bytes | None = None,
    ) -> tuple[RedactedBenchReceipt, bytes]:
        """Execute an internally constructed Docker argv; never accepts caller shell text."""

        argv = ("docker", "exec", "-i", "--workdir", workdir, self.container.name, *command)
        timeout = min(
            self.timeout_seconds,
            self.budget.command_timeout_seconds if self.budget else self.timeout_seconds,
        )
        started_at = _now()
        try:
            completed = subprocess.run(
                argv, shell=False, check=False, cwd=None, capture_output=True,
                text=False, timeout=timeout, input=input_bytes,
            )
        except FileNotFoundError as error:
            raise BenchToolUnavailable(self._tool_unavailable_message()) from error
        except subprocess.TimeoutExpired as error:
            raise BenchCommandTimedOut(
                f"approved Docker operation exceeded the {timeout:g}-second execution limit"
            ) from error
        completed_at = _now()
        stdout, stderr = completed.stdout or b"", completed.stderr or b""
        if self.budget and (
            len(stdout) > self.budget.max_log_bytes
            or len(stderr) > self.budget.max_log_bytes
            or len(stdout) + len(stderr) > self.budget.max_artifact_bytes
        ):
            raise BenchOutputTooLarge("Docker output exceeds the configured log/artifact byte budget")
        receipt = RedactedBenchReceipt(
            argv, completed.returncode, started_at, completed_at,
            len(stdout), hashlib.sha256(stdout).hexdigest(),
            len(stderr), hashlib.sha256(stderr).hexdigest(),
        )
        return receipt, stdout


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_WRITE_GENERATED_ARTIFACT = """\
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import sys
import tempfile

relative, expected, app = sys.argv[1:]
candidate = PurePosixPath(relative)
if candidate.is_absolute() or candidate.as_posix() != relative or '..' in candidate.parts or candidate.parts[0] != app:
    raise SystemExit(72)
content = sys.stdin.buffer.read()
if sha256(content).hexdigest() != expected:
    raise SystemExit(73)
root = (Path.cwd() / 'apps').resolve(strict=True)
destination = (root / relative).resolve(strict=False)
if destination == root or root not in destination.parents:
    raise SystemExit(74)
destination.parent.mkdir(parents=True, exist_ok=True)
handle, temporary = tempfile.mkstemp(prefix='.frappe-harness-', dir=destination.parent)
try:
    with os.fdopen(handle, 'wb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
"""
