from __future__ import annotations

import pytest

from frappe_harness.environment_preflight import CredentialProfile
from frappe_harness.run_store import LockTarget, RunIntent, RunState, RunStore
from frappe_harness.operational_budget import OperationalBudget
from frappe_harness.runtime_lifecycle import RunLifecycleCoordinator, RuntimeLifecycleError


def _run(store: RunStore):
    target = LockTarget("/bench", "dev.local", "task_tracker")
    intent = RunIntent(target, "frappe-native", "a" * 64, "b" * 64)
    run = store.create_run(intent)
    store.transition(run.id, RunState.PENDING_APPROVAL)
    store.record_approval(run.id, approver="operator", approved=True, intent=intent)
    store.transition(run.id, RunState.APPROVED)
    return store.transition(run.id, RunState.RUNNING), target


def test_profile_public_record_never_contains_secret_reference():
    profile = CredentialProfile("harness-live", "frappe-rest", LockTarget("/bench", "dev.local", "task_tracker"), "api_token", "env:FRAPPE_TOKEN", retention_days=7)
    public = profile.public_record()
    assert public["profile_id"] == "harness-live"
    assert "secret_ref" not in public
    assert "FRAPPE_TOKEN" not in repr(public)


def test_profile_lifecycle_is_target_bound_and_durable(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    run, target = _run(store)
    profile = CredentialProfile("harness-live", "frappe-rest", target, "api_token", "env:FRAPPE_TOKEN", retention_days=1)
    coordinator = RunLifecycleCoordinator(store)
    acquired = coordinator.record_credential_profile(run.id, profile)
    released = coordinator.record_credential_profile_release(run.id, profile)
    assert store.get_lifecycle_evidence_payload(acquired.id)["auth_mode"] == "api_token"
    payload = store.get_lifecycle_evidence_payload(released.id)
    assert payload["reason"] == "run_complete"
    assert "secret_ref" not in payload


def test_profile_cannot_cross_run_target_or_accept_unsafe_retention(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    run, _ = _run(store)
    other = LockTarget("/other", "dev.local", "task_tracker")
    with pytest.raises(ValueError):
        CredentialProfile("harness-live", "frappe-rest", other, "api_token", "env:TOKEN", retention_days=31)
    profile = CredentialProfile("harness-live", "frappe-rest", other, "api_token", "env:TOKEN")
    with pytest.raises(RuntimeLifecycleError, match="target"):
        RunLifecycleCoordinator(store).record_credential_profile(run.id, profile)


def test_profile_retention_cannot_exceed_run_budget(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    run, target = _run(store)
    profile = CredentialProfile("harness-live", "frappe-rest", target, "api_token", "env:TOKEN", retention_days=7)
    budget = OperationalBudget(retention_days=3)
    coordinator = RunLifecycleCoordinator(store)
    with pytest.raises(RuntimeLifecycleError, match="retention exceeds"):
        coordinator.record_credential_profile(run.id, profile, budget=budget)
    with pytest.raises(RuntimeLifecycleError, match="retention exceeds"):
        coordinator.record_credential_profile_release(run.id, profile, budget=budget)
