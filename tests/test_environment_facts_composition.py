from __future__ import annotations

from dataclasses import replace

import pytest

from frappe_harness.bench_command_policy import InspectedBenchTarget
from frappe_harness.bench_facts_runner import BenchFactsSnapshot
from frappe_harness.bench_registry_facts import BenchRegistryFacts
from frappe_harness.environment_facts_composition import compose_complete_environment_facts
from frappe_harness.environment_preflight import DiskHealth, EnvironmentIdentity, FrappeRuntime, HealthCheck, ToolStatus
from frappe_harness.frappe_readiness_probe import FrappeReadinessFacts
from frappe_harness.host_facts_runner import HostSupplementalFacts
from frappe_harness.run_store import LockTarget


TARGET = LockTarget("/bench", "site.local", "task_tracker")


def _inputs():
    identity = EnvironmentIdentity(TARGET.bench, TARGET.site, TARGET.app)
    target = InspectedBenchTarget(TARGET.bench, TARGET.site, TARGET.app)
    return (
        BenchFactsSnapshot(identity, FrappeRuntime(16, "16.29.0"), ("frappe", "task_tracker"), True, "mariadb", None),
        HostSupplementalFacts(DiskHealth(2_000_000_000, True), (ToolStatus("bench", True, "5.29.1"),), ()),
        FrappeReadinessFacts(TARGET, (HealthCheck("site", True), HealthCheck("database", True))),
        BenchRegistryFacts(target, False, "ready", "test", "operator"),
    )


def test_composes_complete_facts_without_inference():
    facts = compose_complete_environment_facts(
        TARGET, bench=_inputs()[0], host=_inputs()[1], readiness=_inputs()[2], registry=_inputs()[3],
        source_clean=True, ownership_conflict=False,
    )
    assert facts.production_designated is False
    assert {check.name for check in facts.health_checks} == {"site", "database"}


def test_rejects_registry_target_drift():
    bench, host, readiness, registry = _inputs()
    with pytest.raises(ValueError, match="exact target"):
        compose_complete_environment_facts(
            TARGET, bench=bench, host=host, readiness=readiness,
            registry=replace(registry, target=InspectedBenchTarget("/other", TARGET.site, TARGET.app)),
            source_clean=True, ownership_conflict=False,
        )
