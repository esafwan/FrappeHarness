"""Wire real actor sessions and disposable fixtures into the verification path.

This module is the production boundary that composes:
- identity lifecycle (disposable Frappe users per role)
- REST providers bound to those identities
- fixture provisioning/cleanup via the admin provider
- the run-bound verification adapter

All secrets stay process-local. The module records evidence in the run ledger
but never persists raw credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from .frappe_rest_provider import FrappeApiToken, FrappeRestProvider, FrappeRestTransport, FrappeSessionCookie
from .frappe_verification_probe import CreatedRecord, VerificationFixtures, _doctype, _name_from_payload
from .provider_execution import RunBoundFrappeVerificationAdapter
from .run_store import LockTarget, RunState, RunStore
from .test_identity_lifecycle import DisposableTestIdentityProvider, IdentityLifecycleError, ProvisionedTestIdentity
from .verification_execution import RunBoundBackendVerificationResult
from .verification_program import VerificationProgram


class ActorSessionWiringError(RuntimeError):
    """A run-bound actor session or fixture operation was not admitted."""


class ActorCredentialResolver(Protocol):
    """Resolve a provisioned identity into a process-local credential.

    A production resolver performs the Frappe login call and returns a
    session cookie, or generates an API token for negative permission cases
    that must be reported as ``denied`` rather than ``authentication_required``.
    Tests use a scripted resolver to avoid network I/O.
    """

    def resolve(self, identity: ProvisionedTestIdentity) -> FrappeSessionCookie | FrappeApiToken: ...


@dataclass(frozen=True)
class ActorSessionBundle:
    """The wired actors, fixtures, identities, and fixture records for one run."""

    actors: Mapping[str, FrappeRestProvider]
    fixtures: VerificationFixtures
    identities: tuple[ProvisionedTestIdentity, ...]
    fixture_records: tuple[CreatedRecord, ...]


class RunBoundActorSessionVerification:
    """Provision actor sessions and fixtures, execute verification, and clean up."""

    _DISPOSABLE_ROLES = ("Task Manager", "Task User", "unassigned")

    def __init__(
        self,
        store: RunStore,
        *,
        base_url: str,
        admin_cookie: FrappeSessionCookie,
        identity_provider: DisposableTestIdentityProvider,
        credential_resolver: ActorCredentialResolver,
        rest_transport: FrappeRestTransport | None = None,
    ) -> None:
        self.store = store
        self.base_url = base_url
        self.admin_cookie = admin_cookie
        self.identity_provider = identity_provider
        self.credential_resolver = credential_resolver
        self.rest_transport = rest_transport

    def execute(
        self,
        run_id: str,
        *,
        program: VerificationProgram,
        fixture_template: VerificationFixtures,
    ) -> RunBoundBackendVerificationResult:
        run = self.store.get_run(run_id)
        if run.state is not RunState.RUNNING:
            raise ActorSessionWiringError("actor session verification requires a running approved run")
        if not run.metadata.get("revision_of"):
            raise ActorSessionWiringError("actor session verification requires a successor run")
        if program.spec_hash != run.intent.spec_hash:
            raise ActorSessionWiringError("verification program does not bind the run's approved spec")

        admin_provider = self._admin_provider()
        anonymous_provider = self._anonymous_provider()
        identities: list[ProvisionedTestIdentity] = []
        fixture_records: list[CreatedRecord] = []
        try:
            for role in self._DISPOSABLE_ROLES:
                identities.append(self._provision_identity(run.target, role))
            self._record_identity_provisioned(run_id, run.target, identities)

            actors: dict[str, FrappeRestProvider] = {
                "administrator": admin_provider,
                "anonymous": anonymous_provider,
            }
            for identity in identities:
                actors[identity.role] = self._provider_for_identity(identity)

            fixtures, fixture_records = self._provision_fixtures(
                run_id, admin_provider, program, fixture_template,
            )

            return RunBoundFrappeVerificationAdapter(
                self.store, actors=actors, fixtures=fixtures,
            ).execute_backend(run_id, program=program)
        finally:
            for record in fixture_records:
                try:
                    admin_provider.delete_document(_doctype(record.entity), record.name)
                except Exception:
                    pass
            for identity in identities:
                try:
                    self.identity_provider.cleanup(identity)
                except Exception:
                    pass
            if identities:
                try:
                    target = {"bench": run.target.bench, "site": run.target.site, "app": run.target.app}
                    self.store.record_lifecycle_evidence(run_id, "test_identity_cleaned", {
                        "version": "v1",
                        "target": target,
                        "profile_ids": [identity.profile.profile_id for identity in identities],
                        "credentials_retained": False,
                    })
                    self.store.record_lifecycle_evidence(run_id, "fixture_cleaned", {
                        "version": "v1",
                        "target": target,
                        "fixture_records": [
                            {"actor": record.actor, "entity": record.entity, "name": record.name}
                            for record in fixture_records
                        ],
                    })
                except Exception:
                    pass

    def _provision_identity(self, target: LockTarget, role: str) -> ProvisionedTestIdentity:
        try:
            identity = self.identity_provider.provision(target=target, role=role)
        except IdentityLifecycleError:
            raise
        except Exception as error:
            raise ActorSessionWiringError(f"identity provisioning failed for {role}") from error
        if (
            not isinstance(identity, ProvisionedTestIdentity)
            or identity.profile.target != target
            or identity.role != role
        ):
            raise ActorSessionWiringError(f"identity provider returned an invalid identity for {role}")
        return identity

    def _record_identity_provisioned(
        self, run_id: str, target: LockTarget, identities: list[ProvisionedTestIdentity],
    ) -> None:
        payload = {
            "version": "v1",
            "target": {"bench": target.bench, "site": target.site, "app": target.app},
            "profiles": [
                identity.profile.public_record() | {"role": identity.role}
                for identity in identities
            ],
        }
        self.store.record_lifecycle_evidence(run_id, "test_identity_provisioned", payload)

    def _admin_provider(self) -> FrappeRestProvider:
        return FrappeRestProvider(
            self.base_url,
            None,
            session_cookie=self.admin_cookie,
            transport=self.rest_transport,
        )

    def _anonymous_provider(self) -> FrappeRestProvider:
        return FrappeRestProvider(self.base_url, None, transport=self.rest_transport)

    def _provider_for_identity(self, identity: ProvisionedTestIdentity) -> FrappeRestProvider:
        credential = self.credential_resolver.resolve(identity)
        if isinstance(credential, FrappeApiToken):
            return FrappeRestProvider(
                self.base_url, credential, transport=self.rest_transport,
            )
        return FrappeRestProvider(
            self.base_url, None, session_cookie=credential, transport=self.rest_transport,
        )

    def _provision_fixtures(
        self,
        run_id: str,
        admin_provider: FrappeRestProvider,
        program: VerificationProgram,
        fixture_template: VerificationFixtures,
    ) -> tuple[VerificationFixtures, tuple[CreatedRecord, ...]]:
        record_names = dict(fixture_template.record_names)
        created: list[CreatedRecord] = []
        for entity in fixture_template.valid_payloads:
            payload = fixture_template.payload_for(entity)
            if payload is None:
                continue
            result = admin_provider.create_document(_doctype(entity), payload)
            if not result.ok:
                raise ActorSessionWiringError(f"fixture provisioning failed for {entity}")
            name = _name_from_payload(result.payload)
            if name is None:
                raise ActorSessionWiringError(f"fixture provisioning returned no name for {entity}")
            record_names[entity] = name
            created.append(CreatedRecord("administrator", entity, name))

        delete_names = dict(fixture_template.delete_names)
        for case in program.cases:
            if case.operation != "delete":
                continue
            payload = fixture_template.payload_for(case.entity)
            if payload is None:
                continue
            result = admin_provider.create_document(_doctype(case.entity), payload)
            if not result.ok:
                raise ActorSessionWiringError(f"delete fixture provisioning failed for {case.entity}")
            name = _name_from_payload(result.payload)
            if name is None:
                raise ActorSessionWiringError(
                    f"delete fixture provisioning returned no name for {case.entity}",
                )
            delete_names[case.id] = name
            created.append(CreatedRecord("administrator", case.entity, name))

        fixtures = VerificationFixtures(
            valid_payloads=fixture_template.valid_payloads,
            record_names=record_names,
            update_payloads=fixture_template.update_payloads,
            delete_names=delete_names,
        )
        target = self.store.get_run(run_id).target
        self.store.record_lifecycle_evidence(run_id, "fixture_provisioned", {
            "version": "v1",
            "target": {"bench": target.bench, "site": target.site, "app": target.app},
            "entities": list(record_names.keys()),
            "delete_cases": list(delete_names.keys()),
        })
        return fixtures, tuple(created)
