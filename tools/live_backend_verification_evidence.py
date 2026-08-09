"""Bounded live backend verification evidence acquisition for a V2 successor run.

This operator tool loads an approved schema-v2 operator bundle and its
authorized successor run, reconstructs the hash-bound verification program,
creates process-local least-privilege actor sessions, derives run-local
fixtures from existing records, executes the backend verification report, and
cleans all disposable data.  No secret, cookie, payload, or raw response body is
written to durable output.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Mapping

import requests

from frappe_harness.frappe_rest_provider import (
    DocumentQuery,
    FrappeRestProvider,
    FrappeRestResult,
    FrappeSessionCookie,
    RestErrorKind,
)
from frappe_harness.frappe_verification_probe import (
    CreatedRecord,
    FrappeVerificationProbe,
    VerificationFixtures,
    derive_verification_fixtures,
)
from frappe_harness.operator_bundle import (
    OperatorInputBundle,
    OperatorInputBundleError,
    load_operator_input_bundle,
)
from frappe_harness.operator_input_loaders import (
    OperatorTypedInputError,
    load_confirmed_compilation,
    load_confirmed_plan,
    load_confirmed_preflight,
    load_confirmed_spec,
)
from frappe_harness.provider_execution import (
    ProviderExecutionAdmissionError,
    RunBoundFrappeVerificationAdapter,
)
from frappe_harness.run_store import LockTarget, RunRecord, RunState, RunStore, RunStoreError
from frappe_harness.test_identity_lifecycle import (
    FrappeDisposableTestIdentityProvider,
    FrappeDisposableUnassignedIdentityProvider,
    IdentityLifecycleError,
    ProvisionedTestIdentity,
)
from frappe_harness.test_templates import generate_verification_templates
from frappe_harness.verification_program import (
    VerificationProgram,
    VerificationProgramError,
    load_verification_program,
)


SUCCESSOR_ID = "49582060-c35c-4b98-89b1-dde1cb0d423a"


class LiveBackendVerificationError(RuntimeError):
    """A live backend verification admission or binding check failed."""


class LiveBackendCredentialError(RuntimeError):
    """The process-local credential or session admission failed."""


def _validate_successor_state(successor: RunRecord) -> None:
    """Fail closed unless the successor run is active and approved."""

    if not isinstance(successor, RunRecord):
        raise LiveBackendVerificationError("successor must be a typed RunRecord")
    if successor.state is not RunState.RUNNING:
        raise LiveBackendVerificationError("successor must be in RUNNING state")


def _validate_bundle_target(bundle: OperatorInputBundle, target: LockTarget) -> None:
    """Ensure the bundle names exactly the target the operator supplied."""

    if not isinstance(bundle, OperatorInputBundle) or not isinstance(target, LockTarget):
        raise LiveBackendVerificationError("bundle and target must be typed")
    if bundle.target != target:
        raise LiveBackendVerificationError(
            "operator bundle target does not match the supplied target"
        )


def _validate_successor_intent(bundle: OperatorInputBundle, successor: RunRecord) -> None:
    """Bind the bundle's V2 successor intent to the persisted successor run."""

    if bundle.schema_version != 2 or bundle.successor is None:
        raise LiveBackendVerificationError("successor verification requires a schema-v2 bundle")
    if bundle.successor_intent != successor.intent:
        raise LiveBackendVerificationError(
            "bundle successor intent does not match the persisted successor run"
        )


def _validate_program_binding(program: VerificationProgram, bundle: OperatorInputBundle) -> None:
    """Confirm the generated verification program binds the reconfirmed V2 spec."""

    if program.spec_hash != bundle.reconfirmed_spec_hash:
        raise LiveBackendVerificationError(
            "reconstructed program does not bind the bundle reconfirmed spec hash"
        )


def _load_program_from_bundle(bundle: OperatorInputBundle) -> VerificationProgram:
    """Load confirmed inputs, regenerate templates, and hash-check the program.

    The bundle references are digest-bound; the typed loaders rebuild the typed
    values rather than trusting JSON.  The verification program is then
    reconstructed from the confirmed spec through the deterministic template
    generator and authenticated against the bundle's reconfirmed spec hash.
    """

    spec = load_confirmed_spec(bundle.successor.confirmed_spec)
    plan = load_confirmed_plan(bundle.successor.confirmed_plan, spec)
    compilation = load_confirmed_compilation(bundle.successor.confirmed_compilation, spec)
    templates = generate_verification_templates(spec)
    plan_text = templates.artifacts["harness/verification-plan.json"].decode("utf-8")
    program = load_verification_program(json.loads(plan_text))
    _validate_program_binding(program, bundle)
    # Explicitly acknowledge that the confirmed plan and compilation bind the
    # same spec; their values are loaded only for admission completeness.
    del plan, compilation
    return program


def _admin_session_cookie(base_url: str, password: str) -> FrappeSessionCookie:
    """Login as Administrator and return a typed process-local session cookie."""

    cookie = _login_cookie(base_url, "Administrator", password)
    return FrappeSessionCookie(cookie)


def _user_session_cookie(base_url: str, username: str, password: str) -> FrappeSessionCookie:
    """Login a provisioned disposable user and return a process-local cookie."""

    cookie = _login_cookie(base_url, username, password)
    return FrappeSessionCookie(cookie)


def _login_cookie(base_url: str, username: str, password: str) -> str:
    session = requests.Session()
    response = session.post(
        base_url.rstrip("/") + "/api/method/login",
        data={"usr": username, "pwd": password},
        timeout=20,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        raise LiveBackendCredentialError("login request failed") from error
    cookie = "; ".join(f"{key}={value}" for key, value in session.cookies.get_dict().items())
    if not cookie:
        raise LiveBackendCredentialError("login returned no session cookie")
    return cookie


def _derive_fixtures(
    admin_rest: FrappeRestProvider,
    task_name: str | None,
    program: VerificationProgram,
) -> VerificationFixtures:
    """Fetch one existing Task and its Project, then derive run-local fixtures."""

    task_result = admin_rest.get_document("Task", task_name) if task_name else (
        admin_rest.list_documents("Task", DocumentQuery(limit=1))
    )
    if not task_result.ok or not isinstance(task_result.payload, dict):
        raise LiveBackendVerificationError("typed Task lookup failed")
    data = task_result.payload.get("data")
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
        raise LiveBackendVerificationError("typed Task lookup returned no existing record")
    if set(data) == {"name"}:
        detailed = admin_rest.get_document("Task", data["name"])
        detailed_data = detailed.payload.get("data") if isinstance(detailed.payload, dict) else None
        if detailed.ok and isinstance(detailed_data, dict):
            data = detailed_data

    project_name = data.get("project")
    if not isinstance(project_name, str) or not project_name:
        raise LiveBackendVerificationError("existing Task has no linked Project")
    project_result = admin_rest.get_document("Project", project_name)
    if not project_result.ok or not isinstance(project_result.payload, dict):
        raise LiveBackendVerificationError("typed Project lookup failed")
    project_data = project_result.payload.get("data")
    if not isinstance(project_data, dict) or not isinstance(project_data.get("name"), str):
        raise LiveBackendVerificationError("typed Project lookup returned no existing record")

    records: dict[str, Mapping[str, Any]] = {"task": data, "project": project_data}
    return derive_verification_fixtures(records, case_ids=(case.id for case in program.cases))


def _provision_run_fixtures(
    admin_rest: FrappeRestProvider,
    template: VerificationFixtures,
    program: VerificationProgram,
) -> tuple[VerificationFixtures, list[CreatedRecord]]:
    """Create isolated baseline and delete-case records; never reuse shared data."""
    record_names: dict[str, str] = {}
    delete_names: dict[str, str] = {}
    created: list[CreatedRecord] = []
    for entity, payload in template.valid_payloads.items():
        result = admin_rest.create_document(_entity_to_doctype(entity), payload)
        name = _record_name(result)
        if not result.ok or name is None:
            raise LiveBackendVerificationError(f"fixture creation failed for {entity}")
        record_names[entity] = name
        created.append(CreatedRecord("administrator", entity, name))
    for case in program.cases:
        if case.operation != "delete":
            continue
        payload = template.payload_for(case.entity)
        if payload is None:
            raise LiveBackendVerificationError(f"delete fixture payload missing for {case.entity}")
        result = admin_rest.create_document(_entity_to_doctype(case.entity), payload)
        name = _record_name(result)
        if not result.ok or name is None:
            raise LiveBackendVerificationError(f"delete fixture creation failed for {case.id}")
        delete_names[case.id] = name
        created.append(CreatedRecord("administrator", case.entity, name))
    return VerificationFixtures(
        valid_payloads=template.valid_payloads,
        record_names=record_names,
        update_payloads=template.update_payloads,
        delete_names=delete_names,
    ), created


def _record_name(result: FrappeRestResult) -> str | None:
    payload = result.payload
    if not isinstance(payload, Mapping):
        return None
    data = payload.get("data", payload)
    if isinstance(data, Mapping) and isinstance(data.get("name"), str):
        return data["name"]
    return None


def _entity_to_doctype(entity: str) -> str:
    return " ".join(part.capitalize() for part in entity.split("_"))


_ActorMap = dict[str, FrappeRestProvider]
_IdentityEntry = tuple[ProvisionedTestIdentity, FrappeDisposableTestIdentityProvider]


def _build_actors(
    base_url: str,
    admin_rest: FrappeRestProvider,
    target: LockTarget,
) -> tuple[_ActorMap, list[_IdentityEntry]]:
    """Provision disposable users and create one typed provider per actor role."""

    actors: _ActorMap = {"administrator": admin_rest}
    identity_provider = FrappeDisposableTestIdentityProvider(admin_rest, target=target)
    unassigned_provider = FrappeDisposableUnassignedIdentityProvider(admin_rest, target=target)
    identities: list[_IdentityEntry] = []

    try:
        for role in ("Task User", "Task Manager"):
            identity = identity_provider.provision(target=target, role=role)
            cookie = _user_session_cookie(base_url, identity.username, identity.password)
            actors[role] = FrappeRestProvider(
                base_url, None, session_cookie=cookie,
            )
            identities.append((identity, identity_provider))

        unassigned_identity = unassigned_provider.provision(target=target)
        cookie = _user_session_cookie(
            base_url, unassigned_identity.username, unassigned_identity.password,
        )
        actors["unassigned"] = FrappeRestProvider(
            base_url, None, session_cookie=cookie,
        )
        identities.append((unassigned_identity, unassigned_provider))

        actors["anonymous"] = FrappeRestProvider(base_url, None)
    except Exception:
        # Best-effort cleanup of any partially provisioned identities before
        # re-raising, so a provisioning failure does not leave stray users.
        for identity, provider in identities:
            try:
                provider.cleanup(identity)
            except Exception:
                pass
        raise
    return actors, identities


def _delete_created_record(admin_rest: FrappeRestProvider, record: CreatedRecord) -> bool:
    result = admin_rest.delete_document(_entity_to_doctype(record.entity), record.name)
    if result.ok:
        return True
    if isinstance(result, FrappeRestResult) and result.error_kind is RestErrorKind.NOT_FOUND:
        return True
    return False


def _cleanup(
    admin_rest: FrappeRestProvider,
    probe: FrappeVerificationProbe,
    identities: list[tuple[ProvisionedTestIdentity, FrappeDisposableTestIdentityProvider]],
) -> tuple[int, int]:
    """Delete records created by the probe and all disposable users.

    Failures during cleanup are counted but never raise; the tool must report
    what it could remove without aborting the secret-free summary.
    """

    cleaned_records = 0
    cleaned_users = 0

    # Delete Task records before Project records to avoid Link constraint errors.
    task_records = [record for record in probe.cleanup_records if record.entity == "task"]
    project_records = [record for record in probe.cleanup_records if record.entity == "project"]
    for record in task_records + project_records:
        if _delete_created_record(admin_rest, record):
            cleaned_records += 1

    for identity, provider in identities:
        try:
            provider.cleanup(identity)
            cleaned_users += 1
        except Exception:
            pass

    return cleaned_records, cleaned_users


def _redacted_summary(
    successor_id: str,
    result: Any,
    bundle: OperatorInputBundle,
    created_count: int,
    user_count: int,
    cleaned_records: int,
    cleaned_users: int,
) -> dict[str, Any]:
    """Return a secret-free summary suitable for operator output."""

    return {
        "successor_id": successor_id,
        "parent_run_id": bundle.parent_run_id,
        "backend_verified": result.snapshot is not None,
        "report_id": result.report_record.id,
        "case_count": len(result.report.results),
        "passed": result.report.passed,
        "spec_hash": bundle.reconfirmed_spec_hash,
        "plan_hash": bundle.successor.confirmed_plan.sha256,
        "template_hash": result.report.program_hash,
        "created_record_count": created_count,
        "disposable_user_count": user_count,
        "cleaned_record_count": cleaned_records,
        "cleaned_user_count": cleaned_users,
        "gate_approval_recorded": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--bench-path", required=True)
    parser.add_argument("--app", default="task_tracker")
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--successor-id", default=SUCCESSOR_ID)
    parser.add_argument("--task-name")
    args = parser.parse_args(argv)

    password = os.environ.get("FRAPPE_HARNESS_LIVE_ADMIN_PASSWORD")
    if not password:
        raise SystemExit("FRAPPE_HARNESS_LIVE_ADMIN_PASSWORD must be set in the current process")

    try:
        store = RunStore(args.store)
        bundle = load_operator_input_bundle(args.bundle)
        target = LockTarget(args.bench_path, args.site, args.app)
        _validate_bundle_target(bundle, target)

        successor = store.get_run(args.successor_id)
        _validate_successor_state(successor)
        _validate_successor_intent(bundle, successor)

        program = _load_program_from_bundle(bundle)
        # Load the preflight for admission completeness; its exact target binding
        # is already verified by the typed loader.
        preflight = load_confirmed_preflight(bundle.preflight, target)
        del preflight

        admin_cookie = _admin_session_cookie(args.base_url, password)
        admin_rest = FrappeRestProvider(args.base_url, None, session_cookie=admin_cookie)

        actors, identities = _build_actors(args.base_url, admin_rest, target)
        fixture_template = _derive_fixtures(admin_rest, args.task_name, program)
        fixtures, fixture_records = _provision_run_fixtures(admin_rest, fixture_template, program)

        adapter = RunBoundFrappeVerificationAdapter(store, actors=actors, fixtures=fixtures)
        result = adapter.execute_backend(successor.id, program=program)
        created_count = len(adapter.cleanup_records) + len(fixture_records)
    except (
        OperatorInputBundleError,
        OperatorTypedInputError,
        RunStoreError,
        VerificationProgramError,
        IdentityLifecycleError,
        LiveBackendVerificationError,
        LiveBackendCredentialError,
        ProviderExecutionAdmissionError,
        requests.RequestException,
    ) as error:
        raise SystemExit(f"live backend verification failed: {error}") from error
    finally:
        cleaned_records = cleaned_users = 0
        try:
            if "admin_rest" in locals() and "adapter" in locals():
                cleaned_records, cleaned_users = _cleanup(admin_rest, adapter, identities)
                for fixture in locals().get("fixture_records", []):
                    if _delete_created_record(admin_rest, fixture):
                        cleaned_records += 1
            elif "admin_rest" in locals() and "identities" in locals():
                # A failure before the probe was created may still leave users.
                for identity, provider in identities:
                    try:
                        provider.cleanup(identity)
                        cleaned_users += 1
                    except Exception:
                        pass
        except Exception:
            pass

    summary = _redacted_summary(
        args.successor_id, result, bundle, created_count, len(identities),
        cleaned_records, cleaned_users,
    )
    print(json.dumps(summary, sort_keys=True))
    return 0 if result.report.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
