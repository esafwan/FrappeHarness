import json
from pathlib import Path

from frappe_harness.model_workarounds import WORKAROUNDS, workaround_for
from frappe_harness.route_contracts import Route


def test_every_evaluated_case_has_a_deterministic_guard():
    fixture = json.loads((Path(__file__).parents[1] / "fixtures" / "evaluation" / "v1.json").read_text())
    case_ids = {item["id"] for item in fixture["cases"]}
    assert case_ids == set(WORKAROUNDS)
    assert all(item.guard_code and item.operator_action for item in WORKAROUNDS.values())


def test_workaround_routes_refuse_unsafe_unknown_cases():
    assert workaround_for("DST-004").route is Route.ESCALATE
    assert workaround_for("AMB-003").route is Route.CLARIFY
    assert workaround_for("unknown").route is Route.ESCALATE
    assert workaround_for("unknown").guard_code == "unknown_case"
