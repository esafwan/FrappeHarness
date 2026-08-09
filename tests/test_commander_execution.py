from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from frappe_harness.bench_command_policy import InspectedBenchTarget
from frappe_harness.bench_registry_facts import BenchRegistryFactsRunner
from frappe_harness.commander_execution import (
    CommanderApiToken,
    CommanderBridgeReceipt,
    CommanderMigrationProvider,
    CommanderMutationProviderError,
    CommanderProviderAdmissionError,
    CommanderRestClient,
    CommanderRestClientError,
    CommanderRestTransportFailure,
    CommanderSessionCookie,
    DisposableTargetAttestationError,
    DisposableTargetFacts,
    RegistryDisposableTargetAttestor,
    PINNED_BRIDGE_SUPPORTED_ACTION_KINDS,
    VERIFIED_REST_SUPPORTED_ACTION_KINDS,
)
from frappe_harness.commander_plan import (
    CommanderAction,
    CommanderAddField,
    CommanderCreateDoctype,
    build_commander_compatibility_plan,
)
from frappe_harness.compiler import compile_project
from frappe_harness.contracts import FieldType
from frappe_harness.provider_plan import (
    MetadataPlan,
    ProviderField,
    ProviderOperation,
    build_metadata_plan,
)
from frappe_harness.run_store import LockTarget
from frappe_harness.spec_io import load_project_spec


def _load_task_tracker_spec():
    """Load the task_tracker.json fixture."""
    return load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")


def _build_commander_compatible_plan():
    """Manually construct a minimal MetadataPlan compatible with Commander.

    Since the standard spec validation requires roles and permissions, and Commander
    doesn't support plans with permissions, we manually construct a MetadataPlan with
    operations that have empty permission tuples.
    """
    # Create a minimal operation with no permissions
    operation = ProviderOperation(
        id="create:test_entity",
        kind="create_doctype",
        entity="test_entity",
        doctype="Test Entity",
        module="test_module",
        fields=(
            ProviderField(
                name="title",
                label="Title",
                field_type=FieldType.DATA,
                required=True,
                unique=False,
                default=None,
                select_options=(),
                link_target=None,
            ),
        ),
        permissions=(),  # Empty - this is the key difference
        depends_on=(),
        precondition="doctype_absent:Test Entity",
        postcondition="metadata_matches:test_entity",
    )

    # Create the plan
    plan_hash_input = {
        "version": 1,
        "provider": "commander-spike",
        "target_frappe_major": 16,
        "spec_hash": "minimal_spec_hash_for_testing",
        "operations": [asdict(operation) for operation in [operation]],
    }
    # Create a deterministic hash
    plan_hash = hashlib.sha256(
        json.dumps(plan_hash_input, sort_keys=True).encode("utf-8")
    ).hexdigest()

    return MetadataPlan(
        version=1,
        provider="commander-spike",
        target_frappe_major=16,
        spec_hash="minimal_spec_hash_for_testing",
        operations=(operation,),
        plan_hash=plan_hash,
    )


class FakeAttestor:
    """Fake attestor that tracks calls and can return custom results."""

    def __init__(self, result=None, raise_error=None):
        self.calls = 0
        self.result = result
        self.raise_error = raise_error
        self.captured_target = None

    def attest(self, target: LockTarget) -> DisposableTargetFacts:
        self.calls += 1
        self.captured_target = target
        if self.raise_error:
            raise self.raise_error
        if self.result is None:
            raise AssertionError("should not be called")
        return self.result


class FakeClient:
    """Fake client that tracks calls."""

    def __init__(self, result=None, raise_error=None):
        self.calls = 0
        self.result = result
        self.raise_error = raise_error

    def execute(self, *, target: LockTarget, action) -> CommanderBridgeReceipt:
        self.calls += 1
        if self.raise_error:
            raise self.raise_error
        return self.result


def test_wrong_plan_provider_rejected_before_attestation():
    """Plan with wrong provider is rejected before attestation is called."""
    spec = _load_task_tracker_spec()
    plan = build_metadata_plan(spec, provider="frappe-native")
    compilation = compile_project(spec)

    fake_attestor = FakeAttestor()
    provider = CommanderMigrationProvider(attestor=fake_attestor)

    target = LockTarget("/bench", "dev.local", "task_tracker")
    with pytest.raises(CommanderProviderAdmissionError):
        provider.apply(target=target, plan=plan, compilation=compilation)

    assert fake_attestor.calls == 0


def test_non_disposable_target_rejected_before_translation():
    """Non-disposable target (production_designated=True) is rejected before translation."""
    spec = _load_task_tracker_spec()
    plan = build_metadata_plan(spec, provider="commander-spike")
    compilation = compile_project(spec)

    target = LockTarget("/bench", "dev.local", "task_tracker")
    fake_attestor = FakeAttestor(
        result=DisposableTargetFacts(target, production_designated=True, status="ready")
    )
    fake_client = FakeClient()

    provider = CommanderMigrationProvider(attestor=fake_attestor, client=fake_client)

    with pytest.raises(DisposableTargetAttestationError):
        provider.apply(target=target, plan=plan, compilation=compilation)

    assert fake_client.calls == 0


def test_real_plan_fails_closed_on_unsupported_action_kinds():
    """Plan with unsupported action kinds is rejected before client is called."""
    plan = _build_commander_compatible_plan()
    # For this test, compilation is not actually used, so we'll create a dummy one
    spec = _load_task_tracker_spec()
    compilation = compile_project(spec)

    target = LockTarget("/bench", "dev.local", "task_tracker")
    fake_attestor = FakeAttestor(
        result=DisposableTargetFacts(target, production_designated=False, status="ready")
    )
    fake_client = FakeClient()

    # Pass an empty allowlist to isolate the fail-closed behavior.
    provider = CommanderMigrationProvider(
        attestor=fake_attestor,
        client=fake_client,
        supported_action_kinds=frozenset(),
    )

    with pytest.raises(CommanderProviderAdmissionError, match="create_doctype"):
        provider.apply(target=target, plan=plan, compilation=compilation)

    assert fake_client.calls == 0


def test_missing_client_rejected_after_translation_checks_pass():
    """Missing client is rejected after translation checks pass."""
    plan = _build_commander_compatible_plan()
    spec = _load_task_tracker_spec()
    compilation = compile_project(spec)

    target = LockTarget("/bench", "dev.local", "task_tracker")
    fake_attestor = FakeAttestor(
        result=DisposableTargetFacts(target, production_designated=False, status="ready")
    )

    provider = CommanderMigrationProvider(
        attestor=fake_attestor,
        client=None,
        supported_action_kinds=frozenset({"create_doctype", "add_field"}),
    )

    with pytest.raises(CommanderProviderAdmissionError, match="CommanderBridgeClient"):
        provider.apply(target=target, plan=plan, compilation=compilation)


def test_successful_execution_calls_client_once_per_action_and_returns_digest_only_receipt():
    """Successful execution calls client once per action and returns digest receipt."""
    plan = _build_commander_compatible_plan()
    spec = _load_task_tracker_spec()
    compilation = compile_project(spec)

    target = LockTarget("/bench", "dev.local", "task_tracker")
    fake_attestor = FakeAttestor(
        result=DisposableTargetFacts(target, production_designated=False, status="ready")
    )

    fake_client = FakeClient()
    # Override execute to capture action and return appropriate result
    def execute_impl(*, target, action):
        fake_client.calls += 1
        return CommanderBridgeReceipt(action.id, "ok")

    fake_client.execute = execute_impl

    provider = CommanderMigrationProvider(
        attestor=fake_attestor,
        client=fake_client,
        supported_action_kinds=frozenset({"create_doctype", "add_field"}),
    )

    result = provider.apply(target=target, plan=plan, compilation=compilation)

    # Verify result
    assert result.exit_code == 0

    # Verify client was called once per action
    expected_plan = build_commander_compatibility_plan(plan)
    assert fake_client.calls == len(expected_plan.actions)

    # Verify reference contains plan_hash but not a doctype label
    assert expected_plan.plan_hash in result.reference
    assert "Project" not in result.reference
    assert "Task" not in result.reference


def test_client_exception_never_leaks_raw_text_and_raises_typed_error():
    """Client exceptions are redacted and wrapped in CommanderMutationProviderError."""
    plan = _build_commander_compatible_plan()
    spec = _load_task_tracker_spec()
    compilation = compile_project(spec)

    target = LockTarget("/bench", "dev.local", "task_tracker")
    fake_attestor = FakeAttestor(
        result=DisposableTargetFacts(target, production_designated=False, status="ready")
    )

    fake_client = FakeClient(
        raise_error=RuntimeError("super secret raw stdout: password=hunter2")
    )

    provider = CommanderMigrationProvider(
        attestor=fake_attestor,
        client=fake_client,
        supported_action_kinds=frozenset({"create_doctype", "add_field"}),
    )

    with pytest.raises(CommanderMutationProviderError) as excinfo:
        provider.apply(target=target, plan=plan, compilation=compilation)

    # Verify raw text is not leaked
    assert "hunter2" not in str(excinfo.value)


def test_registry_disposable_target_attestor_wraps_bench_registry_facts():
    """RegistryDisposableTargetAttestor correctly wraps BenchRegistryFactsRunner."""

    def _registry(tmp_path, **entry):
        """Helper to create a registry file."""
        path = tmp_path / "registry.json"
        path.write_text(json.dumps({"version": 1, "benches": {"bench": entry}}), encoding="utf-8")
        return path

    import tempfile
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        # Test case 1: disposable bench should pass
        registry_path = _registry(
            tmp_path,
            path="/bench",
            site_name="dev.local",
            type="disposable",
            status="ready",
            purpose="test",
            signed_by="operator",
        )

        attestor = RegistryDisposableTargetAttestor(BenchRegistryFactsRunner(registry_path))
        facts = attestor.attest(LockTarget("/bench", "dev.local", "task_tracker"))

        assert facts.production_designated is False
        assert facts.status == "ready"

        # Test case 2: persistent bench should fail
        tmp_path2 = Path(tempfile.mkdtemp())
        try:
            registry_path2 = _registry(
                tmp_path2,
                path="/bench",
                site_name="dev.local",
                type="persistent",
                status="ready",
                purpose="test",
                signed_by="operator",
            )

            attestor2 = RegistryDisposableTargetAttestor(BenchRegistryFactsRunner(registry_path2))
            with pytest.raises(DisposableTargetAttestationError):
                attestor2.attest(LockTarget("/bench", "dev.local", "task_tracker"))
        finally:
            import shutil
            shutil.rmtree(tmp_path2, ignore_errors=True)

        # Test case 3: bench path with ".." should fail before reading registry
        attestor_no_file = RegistryDisposableTargetAttestor(
            BenchRegistryFactsRunner(Path("/nonexistent/registry.json"))
        )
        with pytest.raises(DisposableTargetAttestationError):
            attestor_no_file.attest(LockTarget("/bench/../etc", "dev.local", "task_tracker"))



class _FakeHttpResponse:
    """Minimal response stand-in for an injected urllib opener."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self._body


def _capture_opener(captured, status=200, body=b'{"success":true}'):
    """Return a fake opener that records the request and returns a fixed response."""

    def opener(request, timeout):
        captured.append({
            "url": request.full_url,
            "method": request.method,
            "headers": dict(request.headers),
            "body": request.data,
            "timeout": timeout,
        })
        return _FakeHttpResponse(status, body)

    return opener


def _create_action(arguments=None, kind="create_doctype", action_id="action-1"):
    if arguments is None:
        arguments = CommanderCreateDoctype(doctype="Test Entity", module="test_module")
    return CommanderAction(id=action_id, kind=kind, depends_on=(), arguments=arguments)


def _add_field_action(
    name="title",
    label="Title",
    field_type="Data",
    required=False,
    unique=False,
    default=None,
    select_options=(),
    link_target=None,
    after=None,
    action_id="action-add-field",
):
    return CommanderAction(
        id=action_id,
        kind="add_field",
        depends_on=(),
        arguments=CommanderAddField(
            doctype="Test Entity",
            name=name,
            label=label,
            field_type=field_type,
            required=required,
            unique=unique,
            default=default,
            select_options=select_options,
            link_target=link_target,
            after=after,
        ),
    )


def test_default_supported_action_kinds_includes_verified_mapping():
    assert "create_doctype" in PINNED_BRIDGE_SUPPORTED_ACTION_KINDS
    assert "add_field" in PINNED_BRIDGE_SUPPORTED_ACTION_KINDS
    assert PINNED_BRIDGE_SUPPORTED_ACTION_KINDS == VERIFIED_REST_SUPPORTED_ACTION_KINDS


def test_rest_client_create_doctype_posts_to_fixed_endpoint():
    captured = []
    client = CommanderRestClient(
        "https://test.local",
        token=CommanderApiToken("key", "secret"),
        opener=_capture_opener(captured),
    )
    action = _create_action()

    receipt = client.execute(target=LockTarget("/bench", "dev.local", "task_tracker"), action=action)

    assert receipt.status == "ok"
    assert receipt.action_id == action.id
    assert len(captured) == 1
    request = captured[0]
    assert request["url"] == "https://test.local/api/method/commander.api.create_doctype_api"
    assert request["method"] == "POST"
    assert request["headers"]["Authorization"] == "token key:secret"
    content_type = request["headers"].get("Content-Type") or request["headers"].get("Content-type")
    assert content_type == "application/json"
    payload = json.loads(request["body"])
    assert payload == {
        "doctype_name": "Test Entity",
        "module": "test_module",
        "fields": [],
        "custom": False,
    }


def test_rest_client_add_field_posts_commander_field_definition():
    captured = []
    client = CommanderRestClient(
        "https://test.local",
        token=CommanderApiToken("key", "secret"),
        opener=_capture_opener(captured),
    )
    action = _add_field_action(
        name="status",
        label="Status",
        field_type="Select",
        required=True,
        select_options=("Open", "Closed"),
        after="title",
    )

    receipt = client.execute(target=LockTarget("/bench", "dev.local", "task_tracker"), action=action)

    assert receipt.status == "ok"
    request = captured[0]
    assert request["url"] == "https://test.local/api/method/commander.api.add_custom_field_api"
    payload = json.loads(request["body"])
    assert payload["doctype"] == "Test Entity"
    assert payload["insert_after"] == "title"
    # Commander syntax: name:type:*:options=...:label=...
    definition = payload["field_definition"]
    assert definition.startswith("status:Select:")
    assert "*" in definition
    assert "options=Open,Closed" in definition
    assert "label=Status" in definition


def test_rest_client_add_field_link_uses_options():
    captured = []
    client = CommanderRestClient(
        "https://test.local",
        session_cookie=CommanderSessionCookie("sid=abc123"),
        opener=_capture_opener(captured),
    )
    action = _add_field_action(
        name="project",
        label="Project",
        field_type="Link",
        link_target="Project",
    )

    client.execute(target=LockTarget("/bench", "dev.local", "task_tracker"), action=action)

    request = captured[0]
    assert request["headers"]["Cookie"] == "sid=abc123"
    payload = json.loads(request["body"])
    assert "options=Project" in payload["field_definition"]


def test_rest_client_returns_error_receipt_for_commander_failure():
    body = json.dumps({
        "success": False,
        "error": {"message": "DocType already exists", "code": "DOCTYPE_EXISTS"},
    }).encode("utf-8")
    client = CommanderRestClient(
        "https://test.local",
        token=CommanderApiToken("key", "secret"),
        opener=_capture_opener([], status=409, body=body),
    )

    receipt = client.execute(target=LockTarget("/bench", "dev.local", "task_tracker"), action=_create_action())

    assert receipt.status == "error"
    assert receipt.error_code == "DOCTYPE_EXISTS"


def test_rest_client_returns_error_receipt_for_invalid_json():
    client = CommanderRestClient(
        "https://test.local",
        token=CommanderApiToken("key", "secret"),
        opener=_capture_opener([], body=b"not json"),
    )

    receipt = client.execute(target=LockTarget("/bench", "dev.local", "task_tracker"), action=_create_action())

    assert receipt.status == "error"
    assert receipt.error_code == "INVALID_RESPONSE"


def test_rest_client_transport_failure_raises_typed_error():
    from urllib.error import URLError

    def failing_opener(request, timeout):
        raise URLError("no route to host")

    client = CommanderRestClient(
        "https://test.local",
        token=CommanderApiToken("key", "secret"),
        opener=failing_opener,
    )

    with pytest.raises(CommanderRestTransportFailure):
        client.execute(target=LockTarget("/bench", "dev.local", "task_tracker"), action=_create_action())


def test_rest_client_rejects_unsupported_action_kind():
    client = CommanderRestClient(
        "https://test.local",
        token=CommanderApiToken("key", "secret"),
        opener=lambda request, timeout: _FakeHttpResponse(200, b'{"success":true}'),
    )
    action = _create_action(kind="delete_doctype")

    with pytest.raises(CommanderRestClientError, match="not in the verified allowlist"):
        client.execute(target=LockTarget("/bench", "dev.local", "task_tracker"), action=action)


def test_rest_client_rejects_unsupported_field_type():
    client = CommanderRestClient(
        "https://test.local",
        token=CommanderApiToken("key", "secret"),
        opener=lambda request, timeout: _FakeHttpResponse(200, b'{"success":true}'),
    )
    action = _add_field_action(field_type="Text Editor")

    with pytest.raises(CommanderRestClientError, match="not supported by Commander"):
        client.execute(target=LockTarget("/bench", "dev.local", "task_tracker"), action=action)


def test_rest_client_rejects_malformed_origin():
    with pytest.raises(CommanderRestClientError, match="origin only"):
        CommanderRestClient("https://test.local/path", token=CommanderApiToken("key", "secret"))

    with pytest.raises(CommanderRestClientError, match="credentials"):
        CommanderRestClient("https://user:pass@test.local", token=CommanderApiToken("key", "secret"))


def test_rest_client_requires_credentials():
    with pytest.raises(CommanderRestClientError, match="token or a session cookie"):
        CommanderRestClient("https://test.local")


def test_rest_client_typed_credentials_reject_invalid_values():
    with pytest.raises(CommanderRestClientError):
        CommanderApiToken("", "secret")
    with pytest.raises(CommanderRestClientError):
        CommanderApiToken("key", "")
    with pytest.raises(CommanderRestClientError):
        CommanderSessionCookie("")
    with pytest.raises(CommanderRestClientError):
        CommanderSessionCookie("sid=abc\r\ninject")


def test_rest_client_default_allowlist_matches_verified_rest():
    assert CommanderRestClient._SUPPORTED_ACTION_KINDS == VERIFIED_REST_SUPPORTED_ACTION_KINDS
