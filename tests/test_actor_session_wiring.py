from __future__ import annotations

import json
import sqlite3

import pytest

from frappe_harness.actor_session_wiring import ActorSessionWiringError, RunBoundActorSessionVerification
from frappe_harness.environment_preflight import CredentialProfile
from frappe_harness.frappe_rest_provider import FrappeApiToken, FrappeSessionCookie, _WireRequest, _WireResponse
from frappe_harness.frappe_verification_probe import VerificationFixtures
from frappe_harness.lifecycle_contract import LifecycleState
from frappe_harness.run_store import RunIntent, RunState
from frappe_harness.test_identity_lifecycle import ProvisionedTestIdentity
from frappe_harness.verification_program import ProgramCase, VerificationProgram

from test_migration_execution import _prepared_store


class FakeIdentityProvider:
    def __init__(self) -> None:
        self.provisioned: list[ProvisionedTestIdentity] = []
        self.cleaned: list[str] = []

    def provision(self, *, target, role: str) -> ProvisionedTestIdentity:
        profile_id = f"harness_{role.lower().replace(' ', '_')}"
        profile = CredentialProfile(
            profile_id=profile_id,
            provider="frappe-rest",
            target=target,
            auth_mode="session_cookie",
            secret_ref=f"process:{profile_id}",
            retention_days=0,
        )
        identity = ProvisionedTestIdentity(
            profile, role, f"{profile_id}@example.invalid", f"password_{role}",
        )
        self.provisioned.append(identity)
        return identity

    def cleanup(self, identity: ProvisionedTestIdentity) -> None:
        self.cleaned.append(identity.profile.profile_id)


class FakeCredentialResolver:
    def resolve(self, identity: ProvisionedTestIdentity) -> FrappeSessionCookie | FrappeApiToken:
        if identity.role == "unassigned":
            return FrappeApiToken("unassigned_key", "unassigned_secret")
        role = identity.role.lower().replace(" ", "_")
        return FrappeSessionCookie(f"sid={role}")


class FakeTransport:
    def __init__(self, *, fail_for: tuple[str, str] | None = None) -> None:
        self.requests: list[_WireRequest] = []
        self.fail_for = fail_for

    def send(self, request: _WireRequest, *, timeout_seconds: float) -> _WireResponse:
        self.requests.append(request)
        cookie = request.headers.get("Cookie", "")
        authorization = request.headers.get("Authorization", "")
        if self.fail_for and self.fail_for[0] in cookie and request.method == self.fail_for[1]:
            return _WireResponse(500, b"{}")
        if "sid=admin" in cookie:
            if request.method == "POST":
                payload = json.loads(request.body.decode())
                name = payload.get("title", "fixture")
                return _WireResponse(201, json.dumps({"data": {"name": f"created-{name}"}}).encode())
            return _WireResponse(200, b"{}")
        if "sid=task_manager" in cookie or "sid=task_user" in cookie:
            return _WireResponse(200, b'{"data":[]}')
        if authorization.startswith("token "):
            return _WireResponse(403, b"{}")
        return _WireResponse(401, b"{}")


def _prime_successor_lifecycle(store, run_id: str) -> None:
    """Advance a running successor run through METADATA_VERIFIED with minimal evidence."""
    run = store.get_run(run_id)
    target = {"bench": run.target.bench, "site": run.target.site, "app": run.target.app}
    e1 = store.record_lifecycle_evidence(run_id, "environment_preflight", {"eligible": True})
    store.advance_lifecycle_state(run_id, LifecycleState.BENCH_INSPECTED, (e1.id,))
    e2 = store.record_lifecycle_evidence(run_id, "validated_spec", {"spec_hash": run.intent.spec_hash})
    store.advance_lifecycle_state(run_id, LifecycleState.SPEC_VALIDATED, (e2.id,))
    e3 = store.record_lifecycle_evidence(run_id, "compilation", {"spec_hash": run.intent.spec_hash})
    e4 = store.record_lifecycle_evidence(run_id, "artifact_audit", {"valid": True})
    store.advance_lifecycle_state(run_id, LifecycleState.COMPILED, (e3.id, e4.id))
    e5 = store.record_lifecycle_evidence(run_id, "plan", {"plan_hash": run.intent.plan_hash, "spec_hash": run.intent.spec_hash, "provider": run.intent.provider})
    e6 = store.record_lifecycle_evidence(run_id, "approval", {"approval_id": "any"})
    store.advance_lifecycle_state(run_id, LifecycleState.PLAN_APPROVED, (e5.id, e6.id))
    backup = store.record_lifecycle_evidence(run_id, "backup", {
        "recovery_receipt": "v1", "command_receipt_id": "any", "operation": "SiteBackup", "target": target,
    })
    checkpoint = store.record_lifecycle_evidence(run_id, "checkpoint", {
        "recovery_receipt": "v1", "provider_id": "source-checkpoint", "reference": "checkpoint:1",
        "checkpoint": "abc", "target": target, "spec_hash": run.intent.spec_hash, "plan_hash": run.intent.plan_hash,
    })
    e7 = store.record_lifecycle_evidence(run_id, "migration_gate", {
        "plan_hash": run.intent.plan_hash, "spec_hash": run.intent.spec_hash,
        "backup": backup.reference, "checkpoint": checkpoint.reference,
    })
    store.advance_lifecycle_state(run_id, LifecycleState.MIGRATION_GATED, (e7.id, backup.id, checkpoint.id))
    e8 = store.record_lifecycle_evidence(run_id, "migration_receipt", {
        "provider": run.intent.provider, "plan_hash": run.intent.plan_hash,
        "command_receipt_id": "any", "provider_reference": "ref", "gate_revision": 1,
    })
    store.advance_lifecycle_state(run_id, LifecycleState.MIGRATION_APPLIED, (e8.id,))
    e9 = store.record_lifecycle_evidence(run_id, "metadata_verification", {"plan_hash": run.intent.plan_hash, "matches": True})
    store.advance_lifecycle_state(run_id, LifecycleState.METADATA_VERIFIED, (e9.id,))


def _successor_run(tmp_path):
    spec, plan, _compilation, store, run, _approval, _backup, _checkpoint = _prepared_store(tmp_path)
    successor_intent = RunIntent(run.target, run.intent.provider, run.intent.spec_hash, "c" * 64)
    successor = store.supersede_with_revision(run.id, successor_intent)
    store.transition(successor.id, RunState.PENDING_APPROVAL)
    store.record_approval(successor.id, approver="operator", approved=True, intent=successor.intent)
    store.transition(successor.id, RunState.APPROVED)
    store.transition(successor.id, RunState.RUNNING)
    _prime_successor_lifecycle(store, successor.id)
    return store, successor, spec


def _minimal_program(spec_hash: str) -> VerificationProgram:
    return VerificationProgram(
        version=1,
        target_frappe_major=16,
        spec_hash=spec_hash,
        template_hash="a" * 64,
        cases=(
            ProgramCase(
                "permissions:project:read:Task Manager",
                "permissions", "project", "read", "Task Manager", "allowed",
                {"permission": "read"}, (),
            ),
            ProgramCase(
                "permissions:project:read:Task User",
                "permissions", "project", "read", "Task User", "allowed",
                {"permission": "read"}, (),
            ),
            ProgramCase(
                "permissions:project:read:unassigned",
                "permissions", "project", "read", "unassigned", "denied",
                {"permission": "read"}, (),
            ),
            ProgramCase(
                "rest:project:list:Task Manager",
                "rest", "project", "list", "Task Manager", "success",
                {"method": "GET", "permission": "read"}, (),
            ),
            ProgramCase(
                "rest:project:list:Task User",
                "rest", "project", "list", "Task User", "success",
                {"method": "GET", "permission": "read"}, (),
            ),
            ProgramCase(
                "rest:project:list:anonymous",
                "rest", "project", "list", "anonymous", "authentication_required",
                {"method": "GET"}, (),
            ),
        ),
    )


def _fixture_template() -> VerificationFixtures:
    return VerificationFixtures(
        valid_payloads={
            "project": {"title": "_actor_project", "active": True},
            "task": {"title": "_actor_task", "project": "_actor_project", "status": "Open", "priority": "Medium"},
        },
        record_names={},
        update_payloads={
            "project": {"title": "_actor_project_updated"},
            "task": {"title": "_actor_task_updated"},
        },
        delete_names={},
    )


def _list_evidence(store, run_id: str):
    with sqlite3.connect(store.database) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id FROM lifecycle_evidence WHERE run_id = ? ORDER BY created_at, id",
            (run_id,),
        ).fetchall()
    return [store.get_lifecycle_evidence_payload(row["id"]) for row in rows]


def test_successor_run_wires_actors_and_executes_verification(tmp_path):
    store, successor, _spec = _successor_run(tmp_path)
    identity_provider = FakeIdentityProvider()
    transport = FakeTransport()

    result = RunBoundActorSessionVerification(
        store,
        base_url="https://frappe.example.invalid",
        admin_cookie=FrappeSessionCookie("sid=admin"),
        identity_provider=identity_provider,
        credential_resolver=FakeCredentialResolver(),
        rest_transport=transport,
    ).execute(
        successor.id,
        program=_minimal_program(successor.intent.spec_hash),
        fixture_template=_fixture_template(),
    )

    assert result.report.passed
    assert len(result.report.results) == 6
    assert len(identity_provider.provisioned) == 3
    assert set(identity_provider.cleaned) == {
        identity.profile.profile_id for identity in identity_provider.provisioned
    }
    assert any(r.method == "POST" and "/document/Project" in r.url for r in transport.requests)
    assert any(r.method == "POST" and "/document/Task" in r.url for r in transport.requests)
    assert len([r for r in transport.requests if r.method == "DELETE"]) == 2


def test_rejects_non_successor_run(tmp_path):
    _spec, _plan, _compilation, store, run, _approval, _backup, _checkpoint = _prepared_store(tmp_path)
    identity_provider = FakeIdentityProvider()

    with pytest.raises(ActorSessionWiringError, match="successor"):
        RunBoundActorSessionVerification(
            store,
            base_url="https://frappe.example.invalid",
            admin_cookie=FrappeSessionCookie("sid=admin"),
            identity_provider=identity_provider,
            credential_resolver=FakeCredentialResolver(),
        ).execute(
            run.id,
            program=_minimal_program(run.intent.spec_hash),
            fixture_template=_fixture_template(),
        )


def _approved_successor(tmp_path):
    spec, plan, _compilation, store, run, _approval, _backup, _checkpoint = _prepared_store(tmp_path)
    successor_intent = RunIntent(run.target, run.intent.provider, run.intent.spec_hash, "c" * 64)
    successor = store.supersede_with_revision(run.id, successor_intent)
    store.transition(successor.id, RunState.PENDING_APPROVAL)
    store.record_approval(successor.id, approver="operator", approved=True, intent=successor.intent)
    store.transition(successor.id, RunState.APPROVED)
    return store, successor, spec


def test_rejects_running_state_mismatch(tmp_path):
    store, successor, _spec = _approved_successor(tmp_path)
    identity_provider = FakeIdentityProvider()

    with pytest.raises(ActorSessionWiringError, match="running"):
        RunBoundActorSessionVerification(
            store,
            base_url="https://frappe.example.invalid",
            admin_cookie=FrappeSessionCookie("sid=admin"),
            identity_provider=identity_provider,
            credential_resolver=FakeCredentialResolver(),
        ).execute(
            successor.id,
            program=_minimal_program(successor.intent.spec_hash),
            fixture_template=_fixture_template(),
        )


def test_rejects_program_spec_hash_mismatch(tmp_path):
    store, successor, _spec = _successor_run(tmp_path)
    identity_provider = FakeIdentityProvider()
    transport = FakeTransport()

    with pytest.raises(ActorSessionWiringError, match="spec"):
        RunBoundActorSessionVerification(
            store,
            base_url="https://frappe.example.invalid",
            admin_cookie=FrappeSessionCookie("sid=admin"),
            identity_provider=identity_provider,
            credential_resolver=FakeCredentialResolver(),
            rest_transport=transport,
        ).execute(
            successor.id,
            program=_minimal_program("d" * 64),
            fixture_template=_fixture_template(),
        )


def test_records_identity_and_fixture_evidence_without_secrets(tmp_path):
    store, successor, _spec = _successor_run(tmp_path)
    identity_provider = FakeIdentityProvider()
    transport = FakeTransport()

    RunBoundActorSessionVerification(
        store,
        base_url="https://frappe.example.invalid",
        admin_cookie=FrappeSessionCookie("sid=admin"),
        identity_provider=identity_provider,
        credential_resolver=FakeCredentialResolver(),
        rest_transport=transport,
    ).execute(
        successor.id,
        program=_minimal_program(successor.intent.spec_hash),
        fixture_template=_fixture_template(),
    )

    payloads = _list_evidence(store, successor.id)
    assert any(p.get("profiles") for p in payloads)
    provisioned = next(p for p in payloads if "profiles" in p)
    assert provisioned["version"] == "v1"
    for profile in provisioned["profiles"]:
        assert "password" not in profile
        assert "secret" not in profile
    cleaned = next((p for p in payloads if p.get("credentials_retained") is False), None)
    assert cleaned is not None
    assert cleaned["credentials_retained"] is False


def test_fixture_and_identity_cleanup_run_even_on_verification_failure(tmp_path):
    store, successor, _spec = _successor_run(tmp_path)
    identity_provider = FakeIdentityProvider()
    transport = FakeTransport(fail_for=("sid=task_manager", "GET"))

    result = RunBoundActorSessionVerification(
        store,
        base_url="https://frappe.example.invalid",
        admin_cookie=FrappeSessionCookie("sid=admin"),
        identity_provider=identity_provider,
        credential_resolver=FakeCredentialResolver(),
        rest_transport=transport,
    ).execute(
        successor.id,
        program=_minimal_program(successor.intent.spec_hash),
        fixture_template=_fixture_template(),
    )

    assert not result.report.passed
    assert set(identity_provider.cleaned) == {
        identity.profile.profile_id for identity in identity_provider.provisioned
    }
    assert len([r for r in transport.requests if r.method == "DELETE"]) == 2
