"""Compose complete target facts from separately typed, read-only sources."""

from __future__ import annotations

from .bench_facts_runner import BenchFactsSnapshot
from .bench_registry_facts import BenchRegistryFacts
from .environment_preflight import TypedEnvironmentFacts
from .frappe_readiness_probe import FrappeReadinessFacts
from .host_facts_runner import HostSupplementalFacts
from .run_store import LockTarget


class EnvironmentFactsCompositionError(ValueError):
    """Facts are incomplete or disagree about the exact target."""


def compose_complete_environment_facts(
    target: LockTarget,
    *,
    bench: BenchFactsSnapshot,
    host: HostSupplementalFacts,
    readiness: FrappeReadinessFacts,
    registry: BenchRegistryFacts,
    source_clean: bool | None,
    ownership_conflict: bool | None,
) -> TypedEnvironmentFacts:
    """Join only typed observations; no missing fact is inferred."""
    expected = (target.bench, target.site, target.app)
    observations = (
        (bench.identity.bench, bench.identity.site, bench.identity.app),
        (readiness.target.bench, readiness.target.site, readiness.target.app),
        (registry.target.bench_path, registry.target.site_name, registry.target.app_slug),
    )
    if any(value != expected for value in observations):
        raise EnvironmentFactsCompositionError("environment facts do not share the exact target")
    if bench.developer_mode is None or bench.production_designated is not None:
        raise EnvironmentFactsCompositionError("Bench facts must carry explicit developer mode and registry-owned production status")
    if registry.production_designated is not False:
        raise EnvironmentFactsCompositionError("registry does not explicitly attest a non-production target")
    if not readiness.health_checks or any(check.passed is not True for check in readiness.health_checks):
        raise EnvironmentFactsCompositionError("readiness facts must explicitly pass site and database checks")
    return TypedEnvironmentFacts(
        identity=bench.identity,
        frappe=bench.frappe,
        developer_mode=bench.developer_mode,
        production_designated=registry.production_designated,
        installed_apps=bench.installed_apps,
        tools=host.tools,
        disk=host.disk,
        health_checks=readiness.health_checks,
        source_clean=source_clean,
        ownership_conflict=ownership_conflict,
    )
