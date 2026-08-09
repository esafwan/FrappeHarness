"""Disposable, secret-free test-identity lifecycle boundary."""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
import re
import secrets
from typing import Protocol

from .environment_preflight import CredentialProfile
from .frappe_rest_provider import FrappeRestProvider, RestErrorKind
from .run_store import LifecycleEvidenceRecord, LockTarget, RunState, RunStore


class IdentityLifecycleError(RuntimeError):
    """A run-bound test identity operation was not admitted."""


@dataclass(frozen=True)
class ProvisionedTestIdentity:
    profile: CredentialProfile
    role: str
    username: str = ""
    password: str = dataclass_field(repr=False, default="")


class DisposableTestIdentityProvider(Protocol):
    def provision(self, *, target: LockTarget, role: str) -> ProvisionedTestIdentity: ...

    def cleanup(self, identity: ProvisionedTestIdentity) -> None: ...


@dataclass(frozen=True)
class TestIdentityLifecycleResult:
    identities: tuple[ProvisionedTestIdentity, ...]
    provisioned: LifecycleEvidenceRecord
    cleaned: LifecycleEvidenceRecord


class RunBoundTestIdentityLifecycle:
    """Provision and clean identities only inside one approved running run."""

    def __init__(self, store: RunStore, provider: DisposableTestIdentityProvider) -> None:
        self.store = store
        self.provider = provider

    def execute(self, run_id: str, *, roles: tuple[str, ...]) -> TestIdentityLifecycleResult:
        run = self.store.get_run(run_id)
        if run.state is not RunState.RUNNING:
            raise IdentityLifecycleError("test identities require a running approved run")
        if not roles or any(not isinstance(role, str) or not role.strip() for role in roles):
            raise IdentityLifecycleError("roles must be non-blank")
        if len(set(roles)) != len(roles):
            raise IdentityLifecycleError("roles must be unique")
        identities: list[ProvisionedTestIdentity] = []
        try:
            for role in roles:
                identity = self.provider.provision(target=run.target, role=role)
                if not isinstance(identity, ProvisionedTestIdentity) or identity.profile.target != run.target:
                    raise IdentityLifecycleError("provider returned an invalid or mismatched identity")
                if identity.role != role:
                    raise IdentityLifecycleError("provider returned an identity for the wrong role")
                identities.append(identity)
        except Exception as error:
            for identity in identities:
                try:
                    self.provider.cleanup(identity)
                except Exception:
                    pass
            if isinstance(error, IdentityLifecycleError):
                raise
            raise IdentityLifecycleError("test identity provisioning failed") from error
        provisioned = self.store.record_lifecycle_evidence(run_id, "test_identity_provisioned", {
            "version": "v1", "target": run.target.__dict__,
            "profiles": [identity.profile.public_record() | {"role": identity.role} for identity in identities],
        })
        try:
            for identity in identities:
                self.provider.cleanup(identity)
        except Exception as error:
            raise IdentityLifecycleError("test identity cleanup failed") from error
        cleaned = self.store.record_lifecycle_evidence(run_id, "test_identity_cleaned", {
            "version": "v1", "target": run.target.__dict__,
            "profile_ids": [identity.profile.profile_id for identity in identities],
            "credentials_retained": False,
        })
        return TestIdentityLifecycleResult(tuple(identities), provisioned, cleaned)


class FrappeDisposableTestIdentityProvider:
    """Create/delete one least-privilege Frappe User through typed REST."""

    _PROFILE = re.compile(r"^[a-z][a-z0-9_]{0,48}$")
    _FORBIDDEN_ROLES = frozenset({"Administrator", "System Manager", "All"})

    def __init__(self, rest: FrappeRestProvider, *, target: LockTarget, profile_prefix: str = "harness_user") -> None:
        if not isinstance(rest, FrappeRestProvider):
            raise IdentityLifecycleError("disposable provider requires the closed Frappe REST provider")
        if not self._PROFILE.fullmatch(profile_prefix):
            raise IdentityLifecycleError("profile_prefix must be a safe identifier")
        self.rest = rest
        self.target = target
        self.profile_prefix = profile_prefix

    def provision(self, *, target: LockTarget, role: str) -> ProvisionedTestIdentity:
        if target != self.target:
            raise IdentityLifecycleError("identity target does not match the provider target")
        if not isinstance(role, str) or not role.strip() or role in self._FORBIDDEN_ROLES:
            raise IdentityLifecycleError("identity role is not an approved least-privilege role")
        suffix = secrets.token_hex(8)
        username = f"{self.profile_prefix}_{suffix}@example.invalid"
        password = secrets.token_urlsafe(24)
        result = self.rest.create_document("User", {
            "email": username, "first_name": f"Harness {role}", "enabled": 1,
            "new_password": password, "roles": [{"role": role}],
        })
        if not result.ok or not isinstance(result.payload, dict):
            raise IdentityLifecycleError("Frappe disposable user creation failed")
        data = result.payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("name"), str) or data["name"] != username:
            raise IdentityLifecycleError("Frappe disposable user response did not bind the requested identity")
        profile = CredentialProfile(
            profile_id=f"{self.profile_prefix}_{suffix}", provider="frappe-rest",
            target=target, auth_mode="session_cookie", secret_ref=f"process:{self.profile_prefix}_{suffix}",
            retention_days=0,
        )
        return ProvisionedTestIdentity(profile, role, username, password)

    def cleanup(self, identity: ProvisionedTestIdentity) -> None:
        if not isinstance(identity, ProvisionedTestIdentity) or identity.profile.target != self.target:
            raise IdentityLifecycleError("identity cleanup target mismatch")
        result = self.rest.delete_document("User", identity.username)
        if result.ok or result.error_kind is RestErrorKind.NOT_FOUND:
            return
        raise IdentityLifecycleError("Frappe disposable user cleanup failed")


class FrappeDisposableUnassignedIdentityProvider(FrappeDisposableTestIdentityProvider):
    """Provision a disposable user with no roles for negative permission cases."""

    def provision(self, *, target: LockTarget, role: str = "unassigned") -> ProvisionedTestIdentity:
        if target != self.target or role != "unassigned":
            raise IdentityLifecycleError("unassigned identity target or role mismatch")
        suffix = secrets.token_hex(8)
        username = f"{self.profile_prefix}_{suffix}@example.invalid"
        password = secrets.token_urlsafe(24)
        result = self.rest.create_document("User", {
            "email": username, "first_name": "Harness Unassigned", "enabled": 1,
            "new_password": password, "roles": [],
        })
        if not result.ok or not isinstance(result.payload, dict):
            raise IdentityLifecycleError("Frappe unassigned user creation failed")
        data = result.payload.get("data")
        if not isinstance(data, dict) or data.get("name") != username:
            raise IdentityLifecycleError("Frappe unassigned response did not bind identity")
        profile = CredentialProfile(
            profile_id=f"{self.profile_prefix}_{suffix}", provider="frappe-rest",
            target=target, auth_mode="session_cookie", secret_ref=f"process:{self.profile_prefix}_{suffix}",
            retention_days=0,
        )
        return ProvisionedTestIdentity(profile, "unassigned", username, password)
