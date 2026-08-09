from __future__ import annotations

from frappe_harness.local_demo import run_local_demo


def test_local_demo_reaches_ready_with_all_ordered_states(tmp_path):
    receipt = run_local_demo(tmp_path / "demo.sqlite3")
    assert receipt.final_state == "succeeded"
    assert receipt.lifecycle_states == (
        "bench_inspected", "spec_validated", "compiled", "plan_approved",
        "migration_gated", "migration_applied", "metadata_verified", "backend_verified",
        "frontend_verified", "ready",
    )
