from __future__ import annotations

import json

import pytest

from frappe_harness.frappe_readiness_probe import FrappeReadinessProbe, FrappeReadinessProbeError
from frappe_harness.frappe_rest_provider import FrappeRestProvider, FrappeSessionCookie
from frappe_harness.run_store import LockTarget


class Transport:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def send(self, request, *, timeout_seconds):
        from frappe_harness.frappe_rest_provider import _WireResponse
        return _WireResponse(self.status, json.dumps(self.payload).encode())


def test_readiness_probe_derives_site_and_database_checks_from_typed_metadata():
    provider = FrappeRestProvider(
        "http://127.0.0.1:8000", None, session_cookie=FrappeSessionCookie("sid=local"),
        transport=Transport({"data": {"name": "Task", "fields": []}}),
    )
    facts = FrappeReadinessProbe(provider).inspect(LockTarget("/bench", "site.local", "task_tracker"))
    assert facts.target.site == "site.local"
    assert [check.name for check in facts.health_checks] == ["site", "database"]
    assert all(check.passed is True for check in facts.health_checks)


@pytest.mark.parametrize("payload", [{"data": {}}, {"data": {"fields": "invalid"}}])
def test_readiness_probe_rejects_untyped_metadata(payload):
    provider = FrappeRestProvider(
        "http://127.0.0.1:8000", None, session_cookie=FrappeSessionCookie("sid=local"),
        transport=Transport(payload),
    )
    with pytest.raises(FrappeReadinessProbeError):
        FrappeReadinessProbe(provider).inspect(LockTarget("/bench", "site.local", "task_tracker"))
