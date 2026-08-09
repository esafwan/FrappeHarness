from pathlib import Path

from frappe_harness.provider_plan import build_metadata_plan, plan_json
from frappe_harness.spec_io import load_project_spec


def test_native_plan_is_stable_and_orders_link_targets_before_sources():
    spec = load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")

    first = build_metadata_plan(spec)
    second = build_metadata_plan(spec)

    assert first.plan_hash == second.plan_hash
    assert [operation.entity for operation in first.operations] == ["project", "task"]
    assert first.operations[1].depends_on == ("create:project",)
    assert "bench" not in plan_json(first)
    assert "new-doctype" not in plan_json(first)
