from dataclasses import replace

import pytest

from frappe_harness.environment_preflight import (
    DiskHealth,
    EnvironmentIdentity,
    EnvironmentObservation,
    EnvironmentObservationCollector,
    EnvironmentObservationAcquisitionError,
    EnvironmentPreflight,
    EnvironmentPreflightPolicy,
    FrappeRuntime,
    HealthCheck,
    OwnershipObservation,
    TypedEnvironmentObservationAdapter,
    TypedEnvironmentFacts,
    TypedEnvironmentFactsCollector,
    ReadOnlyEnvironmentFactsSource,
    RunnerBackedEnvironmentFactsCollector,
    PayloadEnvironmentFactsSource,
    FrappeEnvironmentFactsSource,
    normalize_environment_facts_payload,
    UnsupportedEnvironmentFact,
    SiteSafety,
    ToolStatus,
    evaluate_environment_preflight,
    evaluate_ownership,
    normalize_environment_observation,
)
from frappe_harness.run_store import LockTarget, RunIntent


def _preflight(**changes):
    value = EnvironmentPreflight(
        identity=EnvironmentIdentity("/srv/bench", "dev.local", "task_tracker"),
        frappe=FrappeRuntime(16, "16.9.0"),
        safety=SiteSafety(developer_mode=True, production_designated=False),
        installed_apps=("frappe", "task_tracker"),
        tools=(ToolStatus("bench", True, "5.29.1"), ToolStatus("frappectl", True, "0.3.0")),
        disk=DiskHealth(2_000_000_000, True),
        health_checks=(HealthCheck("database", True), HealthCheck("site", True)),
    )
    return replace(value, **changes)


def _intent():
    return RunIntent(LockTarget("/srv/bench", "dev.local", "task_tracker"), "frappe-native", "a" * 64, "b" * 64)


def test_complete_development_frappe_16_snapshot_is_eligible_and_binds_intent():
    result = evaluate_environment_preflight(_preflight(), intent=_intent())
    assert result.eligible
    assert result.failures == ()


def test_unknown_or_unsafe_runtime_facts_fail_closed():
    unsafe = _preflight(
        frappe=FrappeRuntime(None),
        safety=SiteSafety(developer_mode=None, production_designated=None),
        disk=DiskHealth(None, None),
        tools=(ToolStatus("bench", None),),
        health_checks=(HealthCheck("database", None),),
        installed_apps=("frappe",),
    )
    result = evaluate_environment_preflight(
        unsafe,
        policy=EnvironmentPreflightPolicy(target_app_state="present"),
    )
    assert not result.eligible
    assert set(result.failure_codes) >= {
        "unsupported_frappe_major",
        "developer_mode_required",
        "production_designation_blocked",
        "target_app_not_installed",
        "required_tool_unavailable",
        "required_tool_unobserved",
        "disk_not_writable",
        "insufficient_disk_space",
        "required_health_check_failed",
        "required_health_check_unobserved",
    }


def test_wrong_frappe_major_production_or_target_identity_never_passes():
    snapshot = _preflight(
        frappe=FrappeRuntime(15),
        safety=SiteSafety(developer_mode=True, production_designated=True),
        identity=EnvironmentIdentity("/other/bench", "dev.local", "task_tracker"),
    )
    result = evaluate_environment_preflight(snapshot, intent=_intent())
    assert not result.eligible
    assert set(result.failure_codes) >= {
        "target_identity_mismatch",
        "unsupported_frappe_major",
        "production_designation_blocked",
    }


def test_policy_can_require_provider_tool_and_set_disk_threshold_without_permissive_defaults():
    policy = EnvironmentPreflightPolicy(
        required_tools=("bench", "frappectl", "frappe_commander"),
        required_health_checks=("database", "site", "redis"),
        minimum_free_bytes=3_000_000_000,
    )
    result = evaluate_environment_preflight(_preflight(), policy=policy)
    assert not result.eligible
    assert set(result.failure_codes) >= {
        "required_tool_unobserved",
        "required_health_check_unobserved",
        "insufficient_disk_space",
    }


def test_target_app_presence_is_operation_specific_not_an_implicit_install_requirement():
    absent = _preflight(installed_apps=("frappe",))
    assert evaluate_environment_preflight(absent).eligible
    assert "target_app_not_installed" in evaluate_environment_preflight(
        absent, policy=EnvironmentPreflightPolicy(target_app_state="present")
    ).failure_codes
    assert "target_app_conflict" in evaluate_environment_preflight(
        _preflight(), policy=EnvironmentPreflightPolicy(target_app_state="absent")
    ).failure_codes


def test_contracts_require_immutable_complete_nonduplicated_observations():
    with pytest.raises(ValueError, match="immutable tuple"):
        _preflight(installed_apps=["frappe", "task_tracker"])
    with pytest.raises(ValueError, match="must not contain duplicates"):
        _preflight(installed_apps=("frappe", "frappe"))
    with pytest.raises(ValueError, match="tool names must be unique"):
        _preflight(tools=(ToolStatus("bench", True), ToolStatus("bench", False)))
    with pytest.raises(ValueError, match="health check names must be unique"):
        _preflight(health_checks=(HealthCheck("site", True), HealthCheck("site", False)))
    with pytest.raises(ValueError, match="unsupported characters"):
        EnvironmentIdentity("/srv/bench", "dev.local", "Task Tracker")


def test_ownership_observation_is_target_bound_and_fail_closed():
    identity = EnvironmentIdentity("/srv/bench", "dev.local", "task_tracker")
    assert evaluate_ownership(OwnershipObservation(identity, True, False), intent=_intent()).eligible
    unknown = evaluate_ownership(OwnershipObservation(identity, None, None), intent=_intent())
    assert not unknown.eligible
    assert set(unknown.failure_codes) == {"source_not_clean", "ownership_conflict"}
    mismatch = evaluate_ownership(
        OwnershipObservation(EnvironmentIdentity("/other", "dev.local", "task_tracker"), True, False), intent=_intent(),
    )
    assert "ownership_target_mismatch" in mismatch.failure_codes


def test_external_collector_boundary_normalizes_one_shared_target_without_io():
    identity = EnvironmentIdentity("/srv/bench", "dev.local", "task_tracker")
    observation = normalize_environment_observation(
        _preflight(), OwnershipObservation(identity, source_clean=True, ownership_conflict=False),
    )

    class Collector:
        def collect(self, target):
            assert target == _intent().target
            return observation

    collector: EnvironmentObservationCollector = Collector()
    assert collector.collect(_intent().target).preflight.identity == identity
    with pytest.raises(ValueError, match="one target identity"):
        normalize_environment_observation(
            _preflight(), OwnershipObservation(EnvironmentIdentity("/other", "dev.local", "task_tracker"), True, False),
        )


def test_typed_external_observation_adapter_fails_closed_on_source_errors_and_target_drift():
    target = _intent().target
    identity = EnvironmentIdentity("/srv/bench", "dev.local", "task_tracker")
    observation = EnvironmentObservation(_preflight(), OwnershipObservation(identity, True, False))

    class Source:
        def collect(self, requested):
            return observation

    assert TypedEnvironmentObservationAdapter(Source()).collect(target) == observation

    class Broken:
        def collect(self, requested):
            raise OSError("read failed")

    with pytest.raises(EnvironmentObservationAcquisitionError, match="failed"):
        TypedEnvironmentObservationAdapter(Broken()).collect(target)

    wrong = EnvironmentObservation(
        replace(_preflight(), identity=EnvironmentIdentity("/other", "dev.local", "task_tracker")),
        OwnershipObservation(EnvironmentIdentity("/other", "dev.local", "task_tracker"), True, False),
    )

    class Drift:
        def collect(self, requested):
            return wrong

    with pytest.raises(EnvironmentObservationAcquisitionError, match="requested target"):
        TypedEnvironmentObservationAdapter(Drift()).collect(target)


def test_typed_facts_collector_normalizes_fixed_read_only_results():
    target = _intent().target
    identity = EnvironmentIdentity("/srv/bench", "dev.local", "task_tracker")
    facts = TypedEnvironmentFacts(
        identity, FrappeRuntime(16, "16.9.0"), True, False,
        ("frappe", "task_tracker"),
        (ToolStatus("bench", True, "5.29.1"), ToolStatus("frappectl", True, "0.3.0")),
        DiskHealth(2_000_000_000, True),
        (HealthCheck("database", True), HealthCheck("site", True)), True, False,
    )
    observation = TypedEnvironmentFactsCollector(facts).collect(target)
    assert observation.preflight.safety.production_designated is False
    assert observation.ownership.source_clean is True
    assert evaluate_environment_preflight(observation.preflight, intent=_intent()).eligible
    assert evaluate_ownership(observation.ownership, intent=_intent()).eligible

    with pytest.raises(EnvironmentObservationAcquisitionError, match="invalid"):
        TypedEnvironmentFactsCollector(replace(facts, installed_apps=["frappe"])).collect(target)

    class Runner:
        def read_facts(self, requested):
            return facts

    source: ReadOnlyEnvironmentFactsSource = Runner()
    assert RunnerBackedEnvironmentFactsCollector(source).collect(target).preflight.frappe.major == 16

    class Unsupported:
        def read_facts(self, requested):
            raise NotImplementedError

    with pytest.raises(UnsupportedEnvironmentFact, match="does not support"):
        RunnerBackedEnvironmentFactsCollector(Unsupported()).collect(target)


def test_environment_facts_payload_normalizer_requires_exact_shape():
    payload = {
        "identity": {"bench": "/srv/bench", "site": "dev.local", "app": "task_tracker"},
        "frappe": {"major": 16, "version": "16.9.0"},
        "safety": {"developer_mode": True, "production_designated": False},
        "installed_apps": ["frappe", "task_tracker"],
        "tools": [{"name": "bench", "available": True, "version": "5.29.1"}, {"name": "frappectl", "available": True, "version": "0.3.0"}],
        "disk": {"free_bytes": 2_000_000_000, "writable": True},
        "health_checks": [{"name": "database", "passed": True, "detail": None}, {"name": "site", "passed": True, "detail": None}],
        "ownership": {"source_clean": True, "ownership_conflict": False},
    }
    facts = normalize_environment_facts_payload(payload)
    assert facts.identity.app == "task_tracker"
    assert PayloadEnvironmentFactsSource(type("Source", (), {"read_payload": lambda _self, _target: payload})()).read_facts(_intent().target) == facts
    with pytest.raises(EnvironmentObservationAcquisitionError, match="approved shape"):
        normalize_environment_facts_payload({**payload, "unexpected": True})
    with pytest.raises(EnvironmentObservationAcquisitionError, match="approved shape"):
        normalize_environment_facts_payload({**payload, "disk": {"free_bytes": 1, "writable": True, "path": "/tmp"}})

    class Client:
        def get_environment_facts(self, site):
            assert site == "dev.local"
            return payload

    assert FrappeEnvironmentFactsSource(Client()).read_payload(_intent().target) == payload

    class Missing:
        def get_environment_facts(self, site):
            raise NotImplementedError

    with pytest.raises(UnsupportedEnvironmentFact, match="unavailable"):
        FrappeEnvironmentFactsSource(Missing()).read_payload(_intent().target)
