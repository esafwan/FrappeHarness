import pytest

from frappe_harness.environment_preflight import DiskHealth, EnvironmentIdentity, EnvironmentPreflight, SiteSafety, ToolStatus
from frappe_harness.frappectl_preflight import (
    FrappectlPayloadError,
    normalize_doctype_show_payload,
    normalize_frappe_runtime_payload,
    normalize_health_check_payload,
    normalize_health_payload,
    normalize_installed_apps_payload,
    normalize_preflight_facts,
    normalize_version_payload,
)


def test_normalizes_explicit_read_payloads_into_portable_preflight_facts():
    facts = normalize_preflight_facts(
        version_payload={"data": {"frappe_version": "16.4.1"}},
        apps_payload={"data": {"installed_apps": ["frappe", "task_tracker"]}},
        health_payload={"data": {"status": "ok"}},
        doctype_payloads={
            "Task": {"data": {"name": "Task", "fields": [], "permissions": []}},
        },
    )

    assert facts.frappe.version == "16.4.1"
    assert facts.frappe.major == 16
    assert facts.installed_apps == ("frappe", "task_tracker")
    assert facts.health_check.name == "site"
    assert facts.health_check.passed is True
    assert facts.doctype_names == ("Task",)

    # The normalized values are actual environment-preflight contract members;
    # callers only need to gather the Bench-local facts that REST cannot prove.
    snapshot = EnvironmentPreflight(
        identity=EnvironmentIdentity("/srv/bench", "dev.local", "task_tracker"),
        frappe=facts.frappe,
        safety=SiteSafety(developer_mode=True, production_designated=False),
        installed_apps=facts.installed_apps,
        tools=(ToolStatus("bench", True), ToolStatus("frappectl", True)),
        disk=DiskHealth(2_000_000_000, True),
        health_checks=(facts.health_check,),
    )
    assert snapshot.health_checks[0].passed is True


def test_normalizes_direct_and_enveloped_typed_payloads():
    assert normalize_version_payload({"version": "v16.0.0-beta.1"}) == ("v16.0.0-beta.1", 16)
    assert normalize_installed_apps_payload({"apps": ["frappe"]}) == ("frappe",)
    assert normalize_health_payload({"healthy": True}) is True
    assert normalize_frappe_runtime_payload({"version": "16.0.0"}).major == 16
    assert normalize_health_check_payload({"status": "healthy"}, name="database").name == "database"
    assert normalize_doctype_show_payload(
        {"name": "Project", "fields": [], "permissions": []}, expected_name="Project"
    )["name"] == "Project"


@pytest.mark.parametrize(
    "payload",
    [
        {"message": ["frappe"]},
        {"apps": []},
        {"apps": ["frappe", "frappe"]},
        {"apps": ["frappe"], "installed_apps": ["erpnext"]},
    ],
)
def test_app_normalizer_rejects_ambiguous_or_untrusted_payloads(payload):
    with pytest.raises(FrappectlPayloadError):
        normalize_installed_apps_payload(payload)


@pytest.mark.parametrize("payload", [{"status": "error"}, {"healthy": False}, {"ok": True}, {"healthy": True, "status": "ok"}])
def test_health_normalizer_requires_one_explicit_healthy_attestation(payload):
    with pytest.raises(FrappectlPayloadError):
        normalize_health_payload(payload)


def test_doctype_normalizer_rejects_wrong_identity_and_non_metadata_shapes():
    with pytest.raises(FrappectlPayloadError, match="does not match"):
        normalize_doctype_show_payload({"data": {"name": "User", "fields": [], "permissions": []}}, expected_name="Task")
    with pytest.raises(FrappectlPayloadError, match="fields"):
        normalize_doctype_show_payload({"name": "Task", "fields": "not-a-list", "permissions": []}, expected_name="Task")


@pytest.mark.parametrize("payload", [{"version": "sixteen"}, {"frappe_version": "16.1", "version": "16.2"}, {"message": "16.0.0"}])
def test_version_normalizer_requires_a_named_semver_like_version(payload):
    with pytest.raises(FrappectlPayloadError):
        normalize_version_payload(payload)
