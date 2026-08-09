from __future__ import annotations

import json

from frappe_harness.environment_facts_io import dump_typed_environment_facts, facts_to_mapping
from frappe_harness.environment_preflight import (
    DiskHealth, EnvironmentIdentity, FrappeRuntime, HealthCheck, SiteSafety, ToolStatus, TypedEnvironmentFacts,
)


def test_environment_facts_serialization_is_canonical_and_secret_free():
    facts = TypedEnvironmentFacts(
        EnvironmentIdentity("/bench", "site.local", "task_tracker"),
        FrappeRuntime(16, "16.29.0"),
        True,
        False,
        ("frappe", "task_tracker"),
        (ToolStatus("bench", True, "5.29.1"),),
        DiskHealth(2_000_000_000, True),
        (HealthCheck("site", True, "digest"), HealthCheck("database", True, "digest")),
        True,
        False,
    )
    encoded = dump_typed_environment_facts(facts)
    assert json.loads(encoded) == facts_to_mapping(facts)
    assert "password" not in encoded
    assert encoded == dump_typed_environment_facts(facts)
