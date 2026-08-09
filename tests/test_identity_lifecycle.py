from __future__ import annotations

from frappe_harness.environment_preflight import CredentialProfile
from frappe_harness.frappe_rest_provider import FrappeRestProvider, FrappeSessionCookie, _WireResponse
from frappe_harness.run_store import LockTarget, RunIntent, RunState, RunStore
from frappe_harness.test_identity_lifecycle import (
    FrappeDisposableTestIdentityProvider,
    FrappeDisposableUnassignedIdentityProvider,
    IdentityLifecycleError,
    ProvisionedTestIdentity,
    RunBoundTestIdentityLifecycle,
    IdentityLifecycleError,
)


def _running(store: RunStore):
    target = LockTarget("/bench", "dev.local", "task_tracker")
    intent = RunIntent(target, "frappe-native", "a" * 64, "b" * 64)
    run = store.create_run(intent)
    store.transition(run.id, RunState.PENDING_APPROVAL)
    store.record_approval(run.id, approver="operator", approved=True, intent=intent)
    store.transition(run.id, RunState.APPROVED)
    return store.transition(run.id, RunState.RUNNING), target


class FakeProvider:
    def __init__(self):
        self.provisioned = []
        self.cleaned = []

    def provision(self, *, target, role):
        identity = ProvisionedTestIdentity(
            CredentialProfile(f"profile_{len(self.provisioned)}", "fake", target, "process_env", "env:TEST_SECRET"),
            role,
        )
        self.provisioned.append(identity)
        return identity

    def cleanup(self, identity):
        self.cleaned.append(identity.profile.profile_id)


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def send(self, request, *, timeout_seconds):
        self.requests.append(request)
        return self.responses.pop(0)


def test_run_bound_identity_lifecycle_records_public_profiles_and_cleanup(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    run, target = _running(store)
    provider = FakeProvider()
    result = RunBoundTestIdentityLifecycle(store, provider).execute(
        run.id, roles=("Task Manager", "Task User")
    )
    assert [identity.role for identity in result.identities] == ["Task Manager", "Task User"]
    assert provider.cleaned == ["profile_0", "profile_1"]
    assert result.provisioned.kind == "test_identity_provisioned"
    assert result.cleaned.kind == "test_identity_cleaned"
    payload = store.get_lifecycle_evidence_payload(result.provisioned.id)
    assert payload["profiles"][0]["profile_id"] == "profile_0"
    assert "secret_ref" not in payload["profiles"][0]


def test_identity_lifecycle_rejects_duplicate_roles_before_provider(tmp_path):
    store = RunStore(tmp_path / "runs.sqlite3")
    run, _ = _running(store)
    provider = FakeProvider()
    try:
        RunBoundTestIdentityLifecycle(store, provider).execute(run.id, roles=("Task User", "Task User"))
    except IdentityLifecycleError as error:
        assert "unique" in str(error)
    else:
        raise AssertionError("duplicate roles must be rejected")
    assert provider.provisioned == []


def test_frappe_provider_uses_typed_user_payload_and_redacts_credentials():
    target = LockTarget("/bench", "dev.local", "task_tracker")
    transport = Transport(_WireResponse(201, b'{"data":{"name":"placeholder"}}'))
    provider = FrappeDisposableTestIdentityProvider(
        FrappeRestProvider("https://example.invalid", None, session_cookie=FrappeSessionCookie("sid=abc"), transport=transport),
        target=target,
    )
    # Bind the response name to the generated email without exposing the secret.
    import json
    class BindingTransport(Transport):
        def send(self, request, *, timeout_seconds):
            payload = json.loads(request.body.decode())
            return _WireResponse(201, json.dumps({"data": {"name": payload["email"]}}).encode())
    provider.rest = FrappeRestProvider("https://example.invalid", None, session_cookie=FrappeSessionCookie("sid=abc"), transport=BindingTransport())
    identity = provider.provision(target=target, role="Task User")
    assert identity.username.endswith("@example.invalid")
    assert identity.password
    assert "new_password" not in repr(identity.profile)
    assert "Administrator" not in identity.role


def test_frappe_provider_rejects_privileged_role_and_target_drift():
    target = LockTarget("/bench", "dev.local", "task_tracker")
    rest = FrappeRestProvider("https://example.invalid", None, session_cookie=FrappeSessionCookie("sid=abc"), transport=Transport())
    provider = FrappeDisposableTestIdentityProvider(rest, target=target)
    try:
        provider.provision(target=target, role="Administrator")
    except IdentityLifecycleError:
        pass
    else:
        raise AssertionError("privileged roles must be rejected")


def test_unassigned_provider_emits_empty_roles_without_privilege_reuse():
    import json
    target = LockTarget("/bench", "dev.local", "task_tracker")
    class BindingTransport(Transport):
        def send(self, request, *, timeout_seconds):
            payload = json.loads(request.body.decode())
            assert payload["roles"] == []
            return _WireResponse(201, json.dumps({"data": {"name": payload["email"]}}).encode())
    provider = FrappeDisposableUnassignedIdentityProvider(
        FrappeRestProvider("https://example.invalid", None, session_cookie=FrappeSessionCookie("sid=abc"), transport=BindingTransport()),
        target=target,
    )
    identity = provider.provision(target=target)
    assert identity.role == "unassigned"
