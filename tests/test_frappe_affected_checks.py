from __future__ import annotations

from dataclasses import dataclass

import pytest

from frappe_harness.frappe_affected_checks import BrowserAttestation, FrappeAffectedCheckConfig, FrappeAffectedCheckProvider
from frappe_harness.run_store import LockTarget, RunStore
from frappe_harness.frappe_rest_provider import FrappeRestResult, RestReceipt


@dataclass
class FakeRest:
    meta: object
    record: object

    def inspect_meta(self, _doctype):
        return self.meta

    def get_document(self, _doctype, _name):
        return self.record


def _ok(payload):
    return FrappeRestResult(RestReceipt("test", 200, 0, "0" * 64), payload, None)


def _provider(rest, browser_probe=None, *, store=None):
    return FrappeAffectedCheckProvider(
        rest,
        FrappeAffectedCheckConfig(
            "/bench", "site.local", "task_tracker", "a" * 64, "b" * 64,
            "TASK-1", {"name": "TASK-1", "title": "Original"},
            browser_probe or (lambda target: BrowserAttestation(target, "https://site.local/task_tracker.html", True)),
        ),
        store=store,
    )


def test_fixed_checks_use_metadata_and_read_preservation_only():
    rest = FakeRest(
        _ok({"data": {"fields": [{"fieldname": "reference_url", "fieldtype": "Data", "options": "URL"}]}}),
        _ok({"data": {"name": "TASK-1", "title": "Original"}}),
    )
    provider = _provider(rest)
    for check in ("migration", "preservation", "browser", "destructive_no_mutation"):
        result = provider.execute("run-1", check)
        assert result["passed"] is True
        assert result["target"] == {"bench": "/bench", "site": "site.local", "app": "task_tracker"}
    assert provider.execute("run-1", "destructive_no_mutation")["mutation_attempted"] is False


def test_metadata_and_record_mismatch_fail_closed():
    provider = _provider(FakeRest(_ok({"data": {"fields": []}}), _ok({"data": {"name": "TASK-1", "title": "Changed"}})))
    assert provider.execute("run-1", "migration")["passed"] is False
    assert provider.execute("run-1", "preservation")["passed"] is False


def test_browser_probe_receives_and_attests_exact_target():
    seen = []
    provider = _provider(
        FakeRest(_ok({"data": {"fields": []}}), _ok({"data": {}})),
        lambda target: (seen.append(target) or BrowserAttestation(target, "https://site.local/task_tracker.html", True)),
    )
    result = provider.execute("run-1", "browser")
    assert result["passed"] is True
    assert seen == [LockTarget("/bench", "site.local", "task_tracker")]
    assert result["browser_attestation"]["target"] == result["target"]


def test_browser_attestation_target_mismatch_fails_closed():
    provider = _provider(
        FakeRest(_ok({"data": {"fields": []}}), _ok({"data": {}})),
        lambda _target: BrowserAttestation(LockTarget("/other", "other.local", "other_app"), "https://other.local", True),
    )
    assert provider.execute("run-1", "browser")["passed"] is False


def test_opaque_bool_browser_probe_fails_closed():
    provider = _provider(
        FakeRest(_ok({"data": {"fields": []}}), _ok({"data": {}})),
        lambda _target: True,
    )
    assert provider.execute("run-1", "browser")["passed"] is False


def test_optional_run_store_binding_is_retained_without_changing_unbound_construction(tmp_path):
    rest = FakeRest(_ok({"data": {"fields": []}}), _ok({"data": {}}))
    store = RunStore(tmp_path / "runs.db")

    assert _provider(rest).store is None
    assert _provider(rest, store=store).store is store

    with pytest.raises(ValueError, match="store must be a RunStore"):
        _provider(rest, store=object())
