"""Canonical serialization for complete secret-free environment facts."""

from __future__ import annotations

import json
from typing import Any

from .environment_preflight import TypedEnvironmentFacts


class EnvironmentFactsIoError(ValueError):
    """Facts are not complete or cannot be represented in the approved shape."""


def facts_to_mapping(facts: TypedEnvironmentFacts) -> dict[str, Any]:
    if not isinstance(facts, TypedEnvironmentFacts):
        raise EnvironmentFactsIoError("facts must be TypedEnvironmentFacts")
    return {
        "identity": {"bench": facts.identity.bench, "site": facts.identity.site, "app": facts.identity.app},
        "frappe": {"major": facts.frappe.major, "version": facts.frappe.version},
        "safety": {"developer_mode": facts.developer_mode, "production_designated": facts.production_designated},
        "installed_apps": list(facts.installed_apps),
        "tools": [
            {"name": tool.name, "available": tool.available, "version": tool.version}
            for tool in facts.tools
        ],
        "disk": {"free_bytes": facts.disk.free_bytes, "writable": facts.disk.writable},
        "health_checks": [
            {"name": check.name, "passed": check.passed, "detail": check.detail}
            for check in facts.health_checks
        ],
        "ownership": {"source_clean": facts.source_clean, "ownership_conflict": facts.ownership_conflict},
    }


def dump_typed_environment_facts(facts: TypedEnvironmentFacts) -> str:
    return json.dumps(facts_to_mapping(facts), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
