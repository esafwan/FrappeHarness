"""Bounded read-only Bench facts acquisition for the BEX-02 spike."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import subprocess
from typing import Any, Callable, Mapping

from .bench_command_policy import InspectedBenchTarget, InspectBenchVersion, SiteListApps, SiteShowConfig, build_bench_argv
from .bench_runner import DockerContainerTarget
from .environment_preflight import (
    DiskHealth,
    EnvironmentIdentity,
    EnvironmentPreflight,
    FrappeRuntime,
    HealthCheck,
    SiteSafety,
    ToolStatus,
)
from .operational_budget import OperationalBudget


class BenchFactsAcquisitionError(RuntimeError):
    """A fixed read-only Bench facts command failed or drifted."""


@dataclass(frozen=True)
class BenchFactsSnapshot:
    """Facts acquired by the bounded Bench CLI adapter.

    The legacy command set deliberately does not pretend to know health, disk,
    or tool availability.  ``to_preflight`` therefore requires those facts
    explicitly from a separately approved read-only source before constructing
    the complete admission contract.
    """

    identity: EnvironmentIdentity
    frappe: FrappeRuntime
    installed_apps: tuple[str, ...]
    developer_mode: bool | None = None
    database_type: str | None = None
    production_designated: bool | None = None

    def to_preflight(
        self,
        *,
        tools: tuple[ToolStatus, ...],
        disk: DiskHealth,
        health_checks: tuple[HealthCheck, ...],
    ) -> EnvironmentPreflight:
        """Compose a complete preflight without inferring unknown facts."""

        if not isinstance(tools, tuple) or not isinstance(disk, DiskHealth) or not isinstance(health_checks, tuple):
            raise BenchFactsAcquisitionError("complete preflight requires typed tool, disk, and health facts")
        return EnvironmentPreflight(
            identity=self.identity,
            frappe=self.frappe,
            safety=SiteSafety(self.developer_mode, self.production_designated),
            installed_apps=self.installed_apps,
            tools=tools,
            disk=disk,
            health_checks=health_checks,
        )


_FRAPPE_VERSION = re.compile(r"^frappe\s+(?P<version>v?[0-9]+(?:\.[0-9]+){0,3}(?:[-+][0-9A-Za-z.-]+)?)\s")


class BenchFactsRunner:
    """Run only version and installed-app read operations, retaining no raw output."""

    def __init__(self, *, timeout_seconds: float = 30.0, budget: OperationalBudget | None = None,
                 process: Callable[..., Any] | None = None) -> None:
        self.timeout_seconds = timeout_seconds
        self.budget = budget
        self._process = process or subprocess.run

    def inspect(self, target: InspectedBenchTarget) -> BenchFactsSnapshot:
        version_output = self._run(InspectBenchVersion(target))
        apps_output = self._run(SiteListApps(target))
        config_output = self._run(SiteShowConfig(target))
        version = _parse_version(version_output)
        apps = _parse_apps(apps_output, target.site_name)
        developer_mode, database_type, production_designated = _parse_site_config(config_output, target.site_name)
        return BenchFactsSnapshot(
            EnvironmentIdentity(target.bench_path, target.site_name, target.app_slug),
            FrappeRuntime(version[1], version[0]), apps, developer_mode, database_type, production_designated,
        )

    def _run(self, operation: object) -> bytes:
        if not isinstance(operation, (InspectBenchVersion, SiteListApps, SiteShowConfig)):
            raise BenchFactsAcquisitionError("only version, installed-app, and site-config operations are supported")
        target = operation.target
        bench_argv = build_bench_argv(operation, inspected_target=target)
        argv, cwd = self._invocation(bench_argv, target)
        timeout = min(self.timeout_seconds, self.budget.command_timeout_seconds if self.budget else self.timeout_seconds)
        try:
            completed = self._process(argv, shell=False, check=False, cwd=cwd,
                                     capture_output=True, text=False, timeout=timeout)
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as error:
            raise BenchFactsAcquisitionError("read-only Bench facts command failed") from error
        stdout = completed.stdout or b""
        stderr = completed.stderr or b""
        if completed.returncode != 0:
            raise BenchFactsAcquisitionError("read-only Bench facts command returned a non-zero exit")
        if self.budget and (len(stdout) > self.budget.max_log_bytes or len(stdout) + len(stderr) > self.budget.max_artifact_bytes):
            raise BenchFactsAcquisitionError("read-only Bench facts output exceeds the evidence budget")
        return stdout

    def _invocation(
        self, bench_argv: tuple[str, ...], target: InspectedBenchTarget,
    ) -> tuple[tuple[str, ...], str | None]:
        """Return the transport argv for one already allowlisted Bench operation."""

        return bench_argv, target.bench_path


class DockerBenchFactsRunner(BenchFactsRunner):
    """Acquire the strict read-only Bench facts snapshot inside one container.

    The facts parser and allowlisted operation set are shared with
    ``BenchFactsRunner``. Docker is only a transport and is constructed here;
    callers cannot provide shell text, an executable, or additional flags.
    """

    def __init__(
        self,
        container: DockerContainerTarget,
        *,
        timeout_seconds: float = 30.0,
        budget: OperationalBudget | None = None,
        process: Callable[..., Any] | None = None,
    ) -> None:
        if not isinstance(container, DockerContainerTarget):
            raise BenchFactsAcquisitionError("container must be a DockerContainerTarget")
        self.container = container
        super().__init__(timeout_seconds=timeout_seconds, budget=budget, process=process)

    def _invocation(
        self, bench_argv: tuple[str, ...], target: InspectedBenchTarget,
    ) -> tuple[tuple[str, ...], None]:
        return (
            ("docker", "exec", "--workdir", target.bench_path, self.container.name, *bench_argv),
            None,
        )


def _parse_version(raw: bytes) -> tuple[str, int]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BenchFactsAcquisitionError("Bench version output is not UTF-8") from error
    matches = [match for line in text.splitlines() if (match := _FRAPPE_VERSION.match(line))]
    if len(matches) != 1:
        raise BenchFactsAcquisitionError("Bench version output does not contain exactly one Frappe version")
    version = matches[0].group("version")
    major = int(version.lstrip("v").split(".", 1)[0])
    return version, major


def _parse_apps(raw: bytes, site_name: str) -> tuple[str, ...]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BenchFactsAcquisitionError("Bench installed-app output is not valid JSON") from error
    if not isinstance(payload, Mapping) or set(payload) != {site_name} or not isinstance(payload[site_name], list):
        raise BenchFactsAcquisitionError("Bench installed-app output has an unsupported shape")
    apps = payload[site_name]
    if not apps or any(not isinstance(app, str) or not app.strip() for app in apps) or len(set(apps)) != len(apps):
        raise BenchFactsAcquisitionError("Bench installed-app output contains invalid app names")
    return tuple(apps)


def _parse_site_config(raw: bytes, site_name: str) -> tuple[bool | None, str | None, bool | None]:
    """Extract only non-secret config facts; all other keys are discarded."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BenchFactsAcquisitionError("Bench site-config output is not valid JSON") from error
    if not isinstance(payload, Mapping) or set(payload) != {site_name} or not isinstance(payload[site_name], Mapping):
        raise BenchFactsAcquisitionError("Bench site-config output has an unsupported shape")
    config = payload[site_name]
    developer = config.get("developer_mode")
    if not isinstance(developer, bool) and (not isinstance(developer, int) or developer not in {0, 1}):
        raise BenchFactsAcquisitionError("developer_mode must be an explicit boolean or 0/1")
    database_type = config.get("db_type")
    if not isinstance(database_type, str) or not database_type.strip():
        raise BenchFactsAcquisitionError("db_type must be an explicit non-blank string")
    production = config.get("production_designated")
    if production is not None and not isinstance(production, bool):
        raise BenchFactsAcquisitionError("production_designated must be an explicit boolean when present")
    return bool(developer), database_type, production
