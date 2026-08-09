from __future__ import annotations

import json

import pytest

from frappe_harness.bench_command_policy import InspectedBenchTarget
from frappe_harness.bench_registry_facts import BenchRegistryFactsError, BenchRegistryFactsRunner


def _registry(tmp_path, **entry):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"version": 1, "benches": {"bench": entry}}), encoding="utf-8")
    return path


def test_registry_attests_only_ready_disposable_public_facts(tmp_path):
    runner = BenchRegistryFactsRunner(_registry(
        tmp_path,
        path="/srv/bench",
        site_name="site.local",
        type="disposable",
        status="ready",
        purpose="harness test",
        signed_by="operator",
        database={"db_password": "must-not-be-read"},
    ))
    facts = runner.inspect(InspectedBenchTarget("/srv/bench", "site.local", "task_tracker"))
    assert facts.production_designated is False
    assert facts.status == "ready"
    assert facts.signed_by == "operator"
    assert "password" not in repr(facts)


@pytest.mark.parametrize("change", [{"type": "persistent"}, {"status": "stopped"}, {"signed_by": ""}])
def test_registry_rejects_non_attested_target(tmp_path, change):
    entry = {
        "path": "/srv/bench", "site_name": "site.local", "type": "disposable", "status": "ready",
        "purpose": "harness test", "signed_by": "operator",
    }
    entry.update(change)
    runner = BenchRegistryFactsRunner(_registry(tmp_path, **entry))
    with pytest.raises(BenchRegistryFactsError):
        runner.inspect(InspectedBenchTarget("/srv/bench", "site.local", "task_tracker"))
