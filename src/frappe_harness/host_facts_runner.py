"""Strict read-only host supplemental facts adapter for disk and tooling.

This module gathers only the environment dimensions that can be observed
through host-local typed APIs: disk usage/writability and the presence/version
of the allowlisted ``bench`` and ``frappectl`` executables.  It never runs
discovery commands, never infers health or production designation, retains no
raw command output, and performs no mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
import shutil
import subprocess
from typing import Any, Callable

from .environment_preflight import DiskHealth, HealthCheck, ToolStatus
from .operational_budget import OperationalBudget


class HostFactsAcquisitionError(RuntimeError):
    """A fixed read-only host facts API failed or drifted."""


@dataclass(frozen=True)
class HostSupplementalFacts:
    """Disk and tooling facts gathered by the bounded host adapter.

    Health checks and production designation are intentionally omitted: this
    adapter only supplies facts it can acquire through the approved typed APIs.
    Callers must obtain health facts from a separately approved source and must
    never infer production status from a path or name.
    """

    disk: DiskHealth
    tools: tuple[ToolStatus, ...]
    health_checks: tuple[HealthCheck, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.disk, DiskHealth):
            raise HostFactsAcquisitionError("disk facts must be a DiskHealth")
        if not isinstance(self.tools, tuple):
            raise HostFactsAcquisitionError("tool facts must be an immutable tuple")
        if not isinstance(self.health_checks, tuple):
            raise HostFactsAcquisitionError("health checks must be an immutable tuple")


_VERSION_RE = re.compile(
    r"v?(?P<version>[0-9]+(?:\.[0-9]+){0,3}(?:[-+][0-9A-Za-z.-]+)?)"
)


class HostFactsRunner:
    """Read-only host facts adapter using only ``shutil.disk_usage``,
    ``os.access``, and allowlisted ``--version`` probes.
    """

    _ALLOWLISTED_TOOLS: tuple[str, ...] = ("bench", "frappectl")

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        budget: OperationalBudget | None = None,
        process: Callable[..., Any] | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.budget = budget
        self._process = process or subprocess.run

    def inspect(self, bench_path: str) -> HostSupplementalFacts:
        """Acquire disk and tool facts for the filesystem hosting ``bench_path``."""

        _validate_bench_path(bench_path)
        disk = self._inspect_disk(bench_path)
        tools = tuple(self._inspect_tool(name) for name in self._ALLOWLISTED_TOOLS)
        # Health and production designation are not inferred from host-local APIs.
        health_checks: tuple[HealthCheck, ...] = ()
        return HostSupplementalFacts(disk=disk, tools=tools, health_checks=health_checks)

    def _inspect_disk(self, bench_path: str) -> DiskHealth:
        try:
            usage = shutil.disk_usage(bench_path)
        except (OSError, ValueError) as error:
            raise HostFactsAcquisitionError("disk usage inspection failed") from error
        writable = os.access(bench_path, os.W_OK)
        return DiskHealth(free_bytes=usage.free, writable=writable)

    def _inspect_tool(self, name: str) -> ToolStatus:
        argv = (name, "--version")
        try:
            completed = self._process(
                argv,
                shell=False,
                check=False,
                capture_output=True,
                text=False,
                timeout=self._timeout(),
            )
        except FileNotFoundError:
            return ToolStatus(name=name, available=False, version=None)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise HostFactsAcquisitionError(f"{name} --version probe failed") from error
        stdout = completed.stdout or b""
        stderr = completed.stderr or b""
        if self.budget and (
            len(stdout) > self.budget.max_log_bytes
            or len(stdout) + len(stderr) > self.budget.max_artifact_bytes
        ):
            raise HostFactsAcquisitionError(
                f"{name} --version output exceeds the evidence budget"
            )
        if completed.returncode != 0:
            return ToolStatus(name=name, available=False, version=None)
        version = _parse_version(stdout)
        return ToolStatus(name=name, available=True, version=version)

    def _timeout(self) -> float:
        if self.budget is None:
            return self.timeout_seconds
        return min(self.timeout_seconds, self.budget.command_timeout_seconds)


def _validate_bench_path(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise HostFactsAcquisitionError("bench_path must be a non-blank trimmed string")
    if not value.startswith("/") or value == "/" or value.endswith("/"):
        raise HostFactsAcquisitionError("bench_path must be a canonical absolute non-root path")
    if any(component in {"", ".", ".."} for component in value.split("/")[1:]):
        raise HostFactsAcquisitionError("bench_path must not contain traversal or empty components")
    return value


def _parse_version(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise HostFactsAcquisitionError("tool version output is not UTF-8") from error
    first_line = text.splitlines()[0] if text else ""
    match = _VERSION_RE.search(first_line)
    if match is None:
        raise HostFactsAcquisitionError("tool version output does not contain a parseable version")
    return match.group("version")
