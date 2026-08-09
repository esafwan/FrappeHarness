"""Fail-closed optional Commander mutation adapter.

Commander (https://github.com/esafwan/frappe_commander) is never trusted as
an authority. This module implements the harness's own `MigrationProvider`
protocol (see migration_execution.py) so that every existing gate --
approval, exact spec/plan/compiler-artifact hash binding, target lock,
backup/checkpoint, durable idempotent command receipts, interrupted-mutation
reconciliation, and post-mutation independent frappectl verification --
applies unmodified. This module adds exactly the things Commander itself
must never be trusted to supply on its own: (1) an exact, registry-attested
disposable-target check that never accepts a caller-supplied bench/site/app
string, (2) a fail-closed allowlist of the exact CommanderAction kinds
Commander's pinned bridge commit is proven able to execute, and (3) a closed,
typed REST client that maps only the verified whitelisted endpoints and
fails closed for permissions/unsupported fields, returning a stable redacted
receipt that never retains raw exception text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from typing import Any, Mapping, Protocol

from .bench_command_policy import InspectedBenchTarget
from .bench_registry_facts import BenchRegistryFactsError, BenchRegistryFactsRunner
from .commander_plan import (
    CommanderAction,
    CommanderAddField,
    CommanderCompatibilityPlan,
    CommanderCreateDoctype,
    CommanderPlanError,
    build_commander_compatibility_plan,
)
from .compiler import CompilationResult
from .migration_execution import MigrationProviderReceipt
from .provider_plan import MetadataPlan
from .run_store import LockTarget


class CommanderProviderAdmissionError(RuntimeError):
    """Commander may not execute this plan; no mutation was attempted."""


class CommanderMutationProviderError(RuntimeError):
    """Commander's bridge failed; only a redacted receipt is retained."""


class DisposableTargetAttestationError(RuntimeError):
    """The exact target is not an attested, ready, non-production disposable Bench."""


@dataclass(frozen=True)
class DisposableTargetFacts:
    """Secret-free attestation that one exact target is disposable and ready."""

    target: LockTarget
    production_designated: bool
    status: str


class DisposableTargetAttestor(Protocol):
    def attest(self, target: LockTarget) -> DisposableTargetFacts:
        """Return exact-target attestation or raise DisposableTargetAttestationError."""


class RegistryDisposableTargetAttestor:
    """Attest a LockTarget by delegating to the existing signed bench registry.

    This never invents attestation logic: it reuses
    `BenchRegistryFactsRunner`, which already proves an exact target is
    registered, disposable, ready, and signed before any mutation may occur.
    """

    def __init__(self, runner: BenchRegistryFactsRunner) -> None:
        self._runner = runner

    def attest(self, target: LockTarget) -> DisposableTargetFacts:
        try:
            inspected = InspectedBenchTarget(target.bench, target.site, target.app)
        except ValueError as error:
            raise DisposableTargetAttestationError("target is not a well-formed Bench identity") from error
        try:
            facts = self._runner.inspect(inspected)
        except BenchRegistryFactsError as error:
            raise DisposableTargetAttestationError("target is not attested by the disposable Bench registry") from error
        return DisposableTargetFacts(target, facts.production_designated, facts.status)


@dataclass(frozen=True)
class CommanderBridgeReceipt:
    """One typed, redacted result from Commander's bridge for one action."""

    action_id: str
    status: str
    error_code: str | None = None


class CommanderBridgeClient(Protocol):
    """A pinned, disposable-only transport to Commander's bridge.

    `CommanderRestClient` below satisfies this protocol using only the two
    verified whitelisted REST endpoints.  Any other adapter must also map a
    fixed, reviewed set of CommanderAction kinds to a pinned endpoint surface
    and must never accept arbitrary URLs, paths, methods, or headers.
    """

    def execute(self, *, target: LockTarget, action) -> CommanderBridgeReceipt:
        ...


@dataclass(frozen=True)
class CommanderApiToken:
    """Typed API token credentials for Commander REST endpoints.

    Callers cannot provide arbitrary headers; authentication is fixed to the
    Frappe ``Authorization: token <key>:<secret>`` scheme.
    """

    key: str = field(repr=False)
    secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key:
            raise CommanderRestClientError("API token key must be a non-empty string")
        if not isinstance(self.secret, str) or not self.secret:
            raise CommanderRestClientError("API token secret must be a non-empty string")


@dataclass(frozen=True)
class CommanderSessionCookie:
    """Typed session cookie for process-local Commander REST authentication."""

    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value.strip() or any(char in self.value for char in "\r\n"):
            raise CommanderRestClientError("session cookie must be a non-blank single-line string")


class CommanderRestClientError(ValueError):
    """A Commander REST client request is outside the fixed typed operation set."""


class CommanderRestTransportFailure(RuntimeError):
    """The Commander REST request did not produce an HTTP response."""


@dataclass(frozen=True)
class _CommanderResponse:
    status: int
    body: bytes


class CommanderRestClient:
    """Closed, typed Commander REST client for the verified endpoint allowlist.

    Only the two endpoints reviewed in the pinned Commander bridge commit are
    reachable: ``create_doctype_api`` and ``add_custom_field_api``.  The caller
    supplies only an origin and typed credentials; method, path, headers, and
    payload keys are fixed here and never taken from the action or target.
    """

    _ENDPOINT_CREATE_DOCTYPE = "create_doctype_api"
    _ENDPOINT_ADD_CUSTOM_FIELD = "add_custom_field_api"
    _SUPPORTED_ACTION_KINDS = frozenset({"create_doctype", "add_field"})
    _SUPPORTED_FIELD_TYPES = frozenset({
        "Data", "Text", "Int", "Float", "Currency", "Date", "Datetime",
        "Check", "Select", "Link",
    })
    _DOCTYPE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{0,139}$")
    _FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
    _MODULE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{0,62}$")

    def __init__(
        self,
        base_url: str,
        *,
        token: CommanderApiToken | None = None,
        session_cookie: CommanderSessionCookie | None = None,
        cookie: str | None = None,
        opener=urlopen,
        timeout_seconds: float = 30.0,
        probe_existing: bool = False,
    ) -> None:
        self._origin = _commander_origin(base_url)
        self._opener = opener
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise CommanderRestClientError("timeout_seconds must be positive")
        self._timeout_seconds = float(timeout_seconds)
        self._probe_existing = probe_existing

        if token is not None:
            if session_cookie is not None or cookie is not None:
                raise CommanderRestClientError("provide token or session cookie, not both")
            if not isinstance(token, CommanderApiToken):
                raise CommanderRestClientError("token must be a CommanderApiToken")
            self._authorization = f"token {token.key}:{token.secret}"
            self._cookie = None
            return

        if session_cookie is not None:
            if cookie is not None:
                raise CommanderRestClientError("provide session_cookie or cookie, not both")
            if not isinstance(session_cookie, CommanderSessionCookie):
                raise CommanderRestClientError("session_cookie must be a CommanderSessionCookie")
            self._authorization = None
            self._cookie = session_cookie.value
            return

        if cookie is not None:
            # Backward-compatible string alias; new code should use CommanderSessionCookie.
            session = CommanderSessionCookie(cookie)
            self._authorization = None
            self._cookie = session.value
            return

        raise CommanderRestClientError("provide either a token or a session cookie")

    def execute(self, *, target: LockTarget, action) -> CommanderBridgeReceipt:
        """Execute one verified CommanderAction against the fixed REST surface.

        ``target`` is received for protocol compatibility and audit binding; the
        client does not use it to select or modify the URL.
        """
        if not isinstance(action, CommanderAction):
            raise CommanderRestClientError("action must be a CommanderAction")
        if action.kind not in self._SUPPORTED_ACTION_KINDS:
            raise CommanderRestClientError(
                f"Commander action kind {action.kind!r} is not in the verified allowlist"
            )
        existing = self._metadata(action.arguments.doctype) if self._probe_existing else None
        if action.kind == "create_doctype" and existing is not None:
            return CommanderBridgeReceipt(action.id, "ok")
        if action.kind == "add_field" and existing is not None:
            fields = existing.get("fields", [])
            if isinstance(fields, list) and any(isinstance(item, dict) and item.get("fieldname") == action.arguments.name for item in fields):
                return CommanderBridgeReceipt(action.id, "ok")
        if self._probe_existing and action.kind == "add_field" and existing is None:
            raise CommanderProviderAdmissionError("Commander field action requires an existing DocType")
        payload = self._payload(action)
        endpoint = (
            self._ENDPOINT_ADD_CUSTOM_FIELD
            if action.kind == "add_field"
            else self._ENDPOINT_CREATE_DOCTYPE
        )
        response = self._post(endpoint, payload)
        return self._to_receipt(action.id, response)

    def _metadata(self, doctype: str) -> dict[str, Any] | None:
        url = f"{self._origin}/api/v2/doctype/{quote(self._doctype_name(doctype), safe='')}/meta"
        headers = {"Accept": "application/json"}
        if self._authorization is not None:
            headers["Authorization"] = self._authorization
        else:
            headers["Cookie"] = self._cookie
        request = Request(url, headers=headers, method="GET")
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                status = int(getattr(response, "status", 200))
                body = response.read()
        except HTTPError as error:
            if error.code == 404:
                return None
            raise CommanderRestTransportFailure("Commander metadata probe failed") from error
        except (URLError, OSError, ValueError) as error:
            raise CommanderRestTransportFailure("Commander metadata probe failed") from error
        if status == 404:
            return None
        if not 200 <= status < 300:
            raise CommanderRestTransportFailure("Commander metadata probe was rejected")
        try:
            decoded = json.loads(body.decode("utf-8"))
            data = decoded.get("data", decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CommanderRestTransportFailure("Commander metadata response was not typed JSON") from error
        if not isinstance(data, dict) or data.get("name") != doctype:
            raise CommanderRestTransportFailure("Commander metadata response was malformed")
        return data

    def _payload(self, action: CommanderAction) -> dict[str, Any]:
        if action.kind == "create_doctype":
            return self._create_doctype_payload(action.arguments)
        if action.kind == "add_field":
            return self._add_field_payload(action.arguments)
        raise CommanderRestClientError(
            f"Commander action kind {action.kind!r} is not in the verified allowlist"
        )

    def _create_doctype_payload(self, arguments: CommanderCreateDoctype) -> dict[str, Any]:
        return {
            "doctype_name": self._doctype_name(arguments.doctype),
            "module": self._module(arguments.module),
            "fields": [],
            "custom": False,
        }

    def _add_field_payload(self, arguments: CommanderAddField) -> dict[str, Any]:
        return {
            "doctype": self._doctype_name(arguments.doctype),
            "field_definition": self._field_definition(arguments),
            "insert_after": arguments.after,
        }

    def _field_definition(self, arguments: CommanderAddField) -> str:
        if arguments.field_type not in self._SUPPORTED_FIELD_TYPES:
            raise CommanderRestClientError(
                f"field type {arguments.field_type!r} is not supported by Commander"
            )
        parts: list[str] = [
            self._field_name(arguments.name),
            arguments.field_type,
        ]
        if arguments.required:
            parts.append("*")
        if arguments.unique:
            parts.append("unique")
        if arguments.field_type == "Select":
            if not arguments.select_options:
                raise CommanderRestClientError("Select field must have options")
            parts.append(f"options={self._select_options(arguments.select_options)}")
        elif arguments.field_type == "Link":
            if not arguments.link_target:
                raise CommanderRestClientError("Link field must have a target")
            parts.append(f"options={self._doctype_name(arguments.link_target)}")
        if arguments.default is not None:
            parts.append(f"?={self._default_value(arguments)}")
        parts.append(f"label={self._label(arguments.label)}")
        return ":".join(parts)

    def _post(self, endpoint: str, payload: Mapping[str, Any]) -> _CommanderResponse:
        url = f"{self._origin}/api/method/commander.api.{endpoint}"
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._authorization is not None:
            headers["Authorization"] = self._authorization
        else:
            headers["Cookie"] = self._cookie
        request = Request(url, data=body, headers=headers, method="POST")
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                return _CommanderResponse(getattr(response, "status", 200), response.read())
        except HTTPError as error:
            return _CommanderResponse(error.code, error.read())
        except (URLError, OSError, ValueError) as error:
            raise CommanderRestTransportFailure("Commander REST transport failed") from error

    def _to_receipt(self, action_id: str, response: _CommanderResponse) -> CommanderBridgeReceipt:
        try:
            decoded = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return CommanderBridgeReceipt(action_id, "error", error_code="INVALID_RESPONSE")
        if not isinstance(decoded, dict):
            return CommanderBridgeReceipt(action_id, "error", error_code="INVALID_RESPONSE")
        if decoded.get("success") is True and 200 <= response.status < 300:
            return CommanderBridgeReceipt(action_id, "ok")
        error = decoded.get("error")
        error_code = None
        if isinstance(error, dict):
            code = error.get("code")
            if isinstance(code, str):
                error_code = code
        return CommanderBridgeReceipt(
            action_id,
            "error",
            error_code=error_code or f"HTTP_{response.status}",
        )

    def _doctype_name(self, value: str) -> str:
        if not isinstance(value, str) or not self._DOCTYPE_NAME_RE.fullmatch(value):
            raise CommanderRestClientError("doctype name is not a safe Commander identifier")
        return value

    def _field_name(self, value: str) -> str:
        if not isinstance(value, str) or not self._FIELD_NAME_RE.fullmatch(value):
            raise CommanderRestClientError("field name is not a safe Commander identifier")
        return value

    def _module(self, value: str) -> str:
        if not isinstance(value, str) or not self._MODULE_RE.fullmatch(value):
            raise CommanderRestClientError("module name is not a safe Commander identifier")
        return value

    def _label(self, value: str) -> str:
        if not isinstance(value, str) or not value.strip() or ":" in value or "|" in value:
            raise CommanderRestClientError("field label cannot be serialized safely in Commander syntax")
        return value.strip()

    def _select_options(self, options: tuple[str, ...]) -> str:
        for option in options:
            if not isinstance(option, str) or not option.strip() or ":" in option or "|" in option:
                raise CommanderRestClientError("Select option cannot be serialized safely in Commander syntax")
        return ",".join(option.strip() for option in options)

    def _default_value(self, arguments: CommanderAddField) -> str:
        field_type = arguments.field_type
        default = arguments.default
        if field_type == "Check":
            return "1" if default else "0"
        if field_type == "Int":
            if isinstance(default, bool) or not isinstance(default, int):
                raise CommanderRestClientError("Int default must be an integer")
            return str(default)
        if field_type in {"Float", "Currency"}:
            if isinstance(default, bool) or not isinstance(default, (int, float)):
                raise CommanderRestClientError("numeric default must be a number")
            return str(float(default))
        if not isinstance(default, str):
            raise CommanderRestClientError("default value must be a string for this field type")
        if ":" in default:
            raise CommanderRestClientError("default value cannot contain ':' in Commander syntax")
        return default


def _commander_origin(base_url: object) -> str:
    if not isinstance(base_url, str):
        raise CommanderRestClientError("base_url must be a string")
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"}:
        raise CommanderRestClientError("base_url scheme must be http or https")
    if not parts.netloc:
        raise CommanderRestClientError("base_url must include a host")
    if parts.username or parts.password:
        raise CommanderRestClientError("base_url must not contain credentials")
    if parts.query or parts.fragment:
        raise CommanderRestClientError("base_url must not contain query or fragment")
    if parts.path not in {"", "/"}:
        raise CommanderRestClientError("base_url must be an origin only")
    return f"{parts.scheme}://{parts.netloc}"


# Exact CommanderAction.kind values Commander's pinned bridge commit is
# proven, by reviewed evidence, able to execute safely end-to-end (dry-run
# diff, idempotent replay, independent read-back).  The verified REST mapping
# implemented above closes the prior gap: create_doctype -> create_doctype_api
# and add_field -> add_custom_field_api, with fail-closed validation for
# permissions/unsupported fields.  Providers that do not pass an explicit
# allowlist continue to fail closed by inheriting this default.
VERIFIED_REST_SUPPORTED_ACTION_KINDS: frozenset[str] = frozenset({"create_doctype", "add_field"})
PINNED_BRIDGE_SUPPORTED_ACTION_KINDS: frozenset[str] = VERIFIED_REST_SUPPORTED_ACTION_KINDS


class CommanderMigrationProvider:
    """Optional Commander-backed `MigrationProvider`. Fails closed by default.

    This class satisfies the exact protocol `RunBoundMigrationExecutor`
    already requires, so every native-path admission gate (provider identity,
    exact spec/plan/compiler-artifact hash binding, MIGRATION_GATED
    prerequisites, single in-flight attempt, interrupted-mutation
    reconciliation) applies unchanged. Independent verification also applies
    unchanged: this provider never reports METADATA_VERIFIED itself, so the
    existing frappectl-backed `RunBoundFrappectlMetadataAdapter` /
    `RunBoundMetadataVerifier` re-inspects the target after this provider
    returns and is the sole authority on whether the mutation matches the
    approved plan.
    """

    provider_id = "commander-spike"

    def __init__(
        self,
        *,
        attestor: DisposableTargetAttestor,
        client: CommanderBridgeClient | None = None,
        supported_action_kinds: frozenset[str] = PINNED_BRIDGE_SUPPORTED_ACTION_KINDS,
    ) -> None:
        self._attestor = attestor
        self._client = client
        self._supported_action_kinds = frozenset(supported_action_kinds)

    def apply(
        self, *, target: LockTarget, plan: MetadataPlan, compilation: CompilationResult,
    ) -> MigrationProviderReceipt:
        if plan.provider != self.provider_id:
            raise CommanderProviderAdmissionError("plan was not built for the commander-spike provider")
        self._attest_disposable_target(target)
        commander_plan = self._translate(plan)
        self._require_supported_actions(commander_plan)
        if self._client is None:
            raise CommanderProviderAdmissionError(
                "no CommanderBridgeClient is configured; Commander's bridge is not "
                "yet exposed on any endpoint this provider may call"
            )
        return self._execute(target, commander_plan)

    def _attest_disposable_target(self, target: LockTarget) -> DisposableTargetFacts:
        try:
            facts = self._attestor.attest(target)
        except DisposableTargetAttestationError:
            raise
        except Exception as error:
            raise DisposableTargetAttestationError("disposable target attestation failed") from error
        if facts.target != target or facts.production_designated or facts.status != "ready":
            raise DisposableTargetAttestationError("target is not an attested, ready, non-production disposable Bench")
        return facts

    def _translate(self, plan: MetadataPlan) -> CommanderCompatibilityPlan:
        try:
            return build_commander_compatibility_plan(plan)
        except CommanderPlanError as error:
            raise CommanderProviderAdmissionError(f"plan is not Commander-safe: {error}") from error

    def _require_supported_actions(self, commander_plan: CommanderCompatibilityPlan) -> None:
        unsupported = sorted({action.kind for action in commander_plan.actions} - self._supported_action_kinds)
        if unsupported:
            raise CommanderProviderAdmissionError(
                "plan requires action kinds Commander's pinned bridge does not support: "
                + ", ".join(unsupported)
            )

    def _execute(self, target: LockTarget, commander_plan: CommanderCompatibilityPlan) -> MigrationProviderReceipt:
        for action in commander_plan.actions:
            try:
                receipt = self._client.execute(target=target, action=action)
            except Exception as error:
                raise CommanderMutationProviderError(f"commander bridge action {action.id} failed") from error
            if receipt.status != "ok":
                raise CommanderMutationProviderError(
                    f"commander bridge action {action.id} reported status {receipt.status!r}"
                )
        reference = f"commander:{commander_plan.plan_hash}:actions:{len(commander_plan.actions)}"
        return MigrationProviderReceipt(reference, 0)
