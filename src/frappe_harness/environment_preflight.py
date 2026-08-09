"""Immutable, pure admission contracts for a Frappe execution environment.

An adapter gathers these facts from a Bench.  This module deliberately does
not inspect paths, run commands, or trust a caller merely because it supplied
a path.  It only decides whether the complete observed snapshot is eligible
for a development-only Frappe 16 operation.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Literal, Mapping, Protocol

from .run_store import LockTarget, RunIntent


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_APP_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _required_text(value: str, label: str, pattern: re.Pattern[str] | None = None) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-blank, trimmed string")
    if pattern is not None and not pattern.fullmatch(value):
        raise ValueError(f"{label} contains unsupported characters")


@dataclass(frozen=True)
class EnvironmentIdentity:
    """Exact target identity reported by an environment inspector."""

    bench: str
    site: str
    app: str

    def __post_init__(self) -> None:
        _required_text(self.bench, "bench")
        _required_text(self.site, "site", _NAME_RE)
        _required_text(self.app, "app", _APP_RE)


_PROFILE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,62}$")
_AUTH_MODES = frozenset({"process_env", "api_token", "session_cookie"})


@dataclass(frozen=True)
class CredentialProfile:
    """Secret-free reference to credentials used by one exact run target.

    The actual secret is resolved by the short-lived provider process.  Only
    this public identity is safe to persist in the run ledger.
    """

    profile_id: str
    provider: str
    target: "LockTarget"
    auth_mode: Literal["process_env", "api_token", "session_cookie"]
    secret_ref: str
    expires_at: str | None = None
    retention_days: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not _PROFILE_RE.fullmatch(self.profile_id):
            raise ValueError("profile_id must be a safe local profile identifier")
        _required_text(self.provider, "credential provider", _PROFILE_RE)
        if not isinstance(self.target, LockTarget):
            raise ValueError("credential profile target must be a LockTarget")
        if self.auth_mode not in _AUTH_MODES:
            raise ValueError("unsupported credential auth mode")
        _required_text(self.secret_ref, "credential secret reference")
        if self.expires_at is not None:
            _required_text(self.expires_at, "credential expiry")
        if isinstance(self.retention_days, bool) or not isinstance(self.retention_days, int) or not 0 <= self.retention_days <= 30:
            raise ValueError("credential retention_days must be between 0 and 30")

    def public_record(self) -> dict[str, object]:
        """Return the only representation permitted in durable evidence."""
        return {
            "profile_id": self.profile_id,
            "provider": self.provider,
            "target": {"bench": self.target.bench, "site": self.target.site, "app": self.target.app},
            "auth_mode": self.auth_mode,
            "expires_at": self.expires_at,
            "retention_days": self.retention_days,
        }


@dataclass(frozen=True)
class FrappeRuntime:
    """Observed Frappe version facts; ``None`` represents unavailable evidence."""

    major: int | None
    version: str | None = None

    def __post_init__(self) -> None:
        if self.major is not None and (not isinstance(self.major, int) or isinstance(self.major, bool) or self.major < 1):
            raise ValueError("Frappe major must be a positive integer or None")
        if self.version is not None:
            _required_text(self.version, "Frappe version")


@dataclass(frozen=True)
class SiteSafety:
    """Observed safety classification, without inferring it from a site name."""

    developer_mode: bool | None
    production_designated: bool | None

    def __post_init__(self) -> None:
        for name, value in (("developer_mode", self.developer_mode), ("production_designated", self.production_designated)):
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"{name} must be bool or None")


@dataclass(frozen=True)
class ToolStatus:
    """One named executable/API capability observed by an adapter."""

    name: str
    available: bool | None
    version: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.name, "tool name", _NAME_RE)
        if self.available is not None and not isinstance(self.available, bool):
            raise ValueError("tool availability must be bool or None")
        if self.version is not None:
            _required_text(self.version, "tool version")


@dataclass(frozen=True)
class DiskHealth:
    """Observed writable-space facts for the target volume."""

    free_bytes: int | None
    writable: bool | None

    def __post_init__(self) -> None:
        if self.free_bytes is not None and (not isinstance(self.free_bytes, int) or isinstance(self.free_bytes, bool) or self.free_bytes < 0):
            raise ValueError("free_bytes must be a non-negative integer or None")
        if self.writable is not None and not isinstance(self.writable, bool):
            raise ValueError("disk writable must be bool or None")


@dataclass(frozen=True)
class HealthCheck:
    """A named health assertion; unknown is represented by ``passed=None``."""

    name: str
    passed: bool | None
    detail: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.name, "health check name", _NAME_RE)
        if self.passed is not None and not isinstance(self.passed, bool):
            raise ValueError("health check passed must be bool or None")
        if self.detail is not None:
            _required_text(self.detail, "health check detail")


def _unique_names(items: Iterable[object], label: str) -> None:
    names = tuple(getattr(item, "name") for item in items)
    if len(names) != len(set(names)):
        raise ValueError(f"{label} names must be unique")


@dataclass(frozen=True)
class EnvironmentPreflight:
    """The complete immutable observation used by the eligibility policy."""

    identity: EnvironmentIdentity
    frappe: FrappeRuntime
    safety: SiteSafety
    installed_apps: tuple[str, ...]
    tools: tuple[ToolStatus, ...]
    disk: DiskHealth
    health_checks: tuple[HealthCheck, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.installed_apps, tuple):
            raise ValueError("installed_apps must be an immutable tuple")
        for app in self.installed_apps:
            _required_text(app, "installed app", _APP_RE)
        if len(self.installed_apps) != len(set(self.installed_apps)):
            raise ValueError("installed_apps must not contain duplicates")
        if not isinstance(self.tools, tuple) or not isinstance(self.health_checks, tuple):
            raise ValueError("tools and health_checks must be immutable tuples")
        _unique_names(self.tools, "tool")
        _unique_names(self.health_checks, "health check")


@dataclass(frozen=True)
class OwnershipObservation:
    """Typed, secret-free source/app ownership facts supplied by an inspector."""

    identity: EnvironmentIdentity
    source_clean: bool | None
    ownership_conflict: bool | None

    def __post_init__(self) -> None:
        for label, value in (("source_clean", self.source_clean), ("ownership_conflict", self.ownership_conflict)):
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"{label} must be bool or None")


@dataclass(frozen=True)
class OwnershipEligibility:
    """Fail-closed result for source ownership checks."""

    eligible: bool
    failures: tuple[PreflightFailure, ...]

    @property
    def failure_codes(self) -> tuple[str, ...]:
        return tuple(failure.code for failure in self.failures)


@dataclass(frozen=True)
class EnvironmentObservation:
    """Normalized, immutable facts returned by an external inspector."""

    preflight: EnvironmentPreflight
    ownership: OwnershipObservation

    def __post_init__(self) -> None:
        if self.preflight.identity != self.ownership.identity:
            raise ValueError("preflight and ownership observations must share one target identity")


class EnvironmentObservationCollector(Protocol):
    """Capability-free seam for an external Bench inspector implementation."""

    def collect(self, target: LockTarget) -> EnvironmentObservation: ...


class EnvironmentObservationAcquisitionError(RuntimeError):
    """A typed external observation source could not provide safe facts."""


class TypedEnvironmentObservationAdapter:
    """Fail-closed adapter for an injected, read-only observation source."""

    def __init__(self, source: EnvironmentObservationCollector) -> None:
        self.source = source

    def collect(self, target: LockTarget) -> EnvironmentObservation:
        if not isinstance(target, LockTarget):
            raise EnvironmentObservationAcquisitionError("collector target must be a LockTarget")
        try:
            observation = self.source.collect(target)
        except Exception as error:
            raise EnvironmentObservationAcquisitionError("external environment observation failed") from error
        if not isinstance(observation, EnvironmentObservation):
            raise EnvironmentObservationAcquisitionError("external source returned an invalid observation")
        expected = (target.bench, target.site, target.app)
        actual = (
            observation.preflight.identity.bench,
            observation.preflight.identity.site,
            observation.preflight.identity.app,
        )
        if actual != expected:
            raise EnvironmentObservationAcquisitionError("external observation does not match the requested target")
        return normalize_environment_observation(observation.preflight, observation.ownership)


@dataclass(frozen=True)
class TypedEnvironmentFacts:
    """Results from fixed, read-only commands normalized before collection."""

    identity: EnvironmentIdentity
    frappe: FrappeRuntime
    developer_mode: bool | None
    production_designated: bool | None
    installed_apps: tuple[str, ...]
    tools: tuple[ToolStatus, ...]
    disk: DiskHealth
    health_checks: tuple[HealthCheck, ...]
    source_clean: bool | None
    ownership_conflict: bool | None


class TypedEnvironmentFactsCollector:
    """Concrete collector over injected typed command results; it performs no I/O."""

    def __init__(self, facts: TypedEnvironmentFacts) -> None:
        self.facts = facts

    def collect(self, target: LockTarget) -> EnvironmentObservation:
        if not isinstance(target, LockTarget):
            raise EnvironmentObservationAcquisitionError("collector target must be a LockTarget")
        expected = (target.bench, target.site, target.app)
        actual = (self.facts.identity.bench, self.facts.identity.site, self.facts.identity.app)
        if actual != expected:
            raise EnvironmentObservationAcquisitionError("typed facts do not match the requested target")
        try:
            preflight = EnvironmentPreflight(
                identity=self.facts.identity,
                frappe=self.facts.frappe,
                safety=SiteSafety(self.facts.developer_mode, self.facts.production_designated),
                installed_apps=self.facts.installed_apps,
                tools=self.facts.tools,
                disk=self.facts.disk,
                health_checks=self.facts.health_checks,
            )
            ownership = OwnershipObservation(self.facts.identity, self.facts.source_clean, self.facts.ownership_conflict)
            return normalize_environment_observation(preflight, ownership)
        except (TypeError, ValueError) as error:
            raise EnvironmentObservationAcquisitionError("typed environment facts are invalid") from error


class UnsupportedEnvironmentFact(EnvironmentObservationAcquisitionError):
    """A read-only runner cannot provide one required environment fact."""


class ReadOnlyEnvironmentFactsSource(Protocol):
    """Adapter contract for composing fixed Bench/frappectl read operations."""

    def read_facts(self, target: LockTarget) -> TypedEnvironmentFacts: ...


class RunnerBackedEnvironmentFactsCollector:
    """Normalize facts from an injected, read-only runner composition."""

    def __init__(self, source: ReadOnlyEnvironmentFactsSource) -> None:
        self.source = source

    def collect(self, target: LockTarget) -> EnvironmentObservation:
        try:
            facts = self.source.read_facts(target)
        except NotImplementedError as error:
            raise UnsupportedEnvironmentFact("read-only runner does not support required environment facts") from error
        except Exception as error:
            raise EnvironmentObservationAcquisitionError("read-only environment facts source failed") from error
        if not isinstance(facts, TypedEnvironmentFacts):
            raise EnvironmentObservationAcquisitionError("read-only source returned invalid typed facts")
        return TypedEnvironmentFactsCollector(facts).collect(target)


class ReadOnlyEnvironmentPayloadSource(Protocol):
    """Read-only source returning one fixed environment JSON payload."""

    def read_payload(self, target: LockTarget) -> Mapping[str, Any]: ...


class FrappeEnvironmentFactsClient(Protocol):
    """Injected read-only Frappe facts endpoint/client contract."""

    def get_environment_facts(self, site: str) -> Mapping[str, Any]: ...


def normalize_environment_facts_payload(payload: Mapping[str, Any]) -> TypedEnvironmentFacts:
    """Normalize the exact external facts shape; reject drift or unknown keys."""

    if not isinstance(payload, Mapping):
        raise EnvironmentObservationAcquisitionError("environment facts payload must be an object")
    _exact_keys(payload, {"identity", "frappe", "safety", "installed_apps", "tools", "disk", "health_checks", "ownership"}, "facts")
    identity_value = _object(payload["identity"], "identity", {"bench", "site", "app"})
    identity = EnvironmentIdentity(identity_value["bench"], identity_value["site"], identity_value["app"])
    frappe_value = _object(payload["frappe"], "frappe", {"major", "version"})
    safety_value = _object(payload["safety"], "safety", {"developer_mode", "production_designated"})
    disk_value = _object(payload["disk"], "disk", {"free_bytes", "writable"})
    ownership_value = _object(payload["ownership"], "ownership", {"source_clean", "ownership_conflict"})
    installed = payload["installed_apps"]
    if not isinstance(installed, list):
        raise EnvironmentObservationAcquisitionError("installed_apps must be a list")
    tools = tuple(_tool(item) for item in _list(payload["tools"], "tools"))
    health = tuple(_health(item) for item in _list(payload["health_checks"], "health_checks"))
    try:
        return TypedEnvironmentFacts(
            identity=identity,
            frappe=FrappeRuntime(frappe_value["major"], frappe_value["version"]),
            developer_mode=safety_value["developer_mode"],
            production_designated=safety_value["production_designated"],
            installed_apps=tuple(installed), tools=tools,
            disk=DiskHealth(disk_value["free_bytes"], disk_value["writable"]),
            health_checks=health,
            source_clean=ownership_value["source_clean"],
            ownership_conflict=ownership_value["ownership_conflict"],
        )
    except (TypeError, ValueError, KeyError) as error:
        raise EnvironmentObservationAcquisitionError("environment facts payload is malformed") from error


class PayloadEnvironmentFactsSource:
    """Adapt a fixed read-only payload source into the runner-backed collector."""

    def __init__(self, source: ReadOnlyEnvironmentPayloadSource) -> None:
        self.source = source

    def read_facts(self, target: LockTarget) -> TypedEnvironmentFacts:
        try:
            payload = self.source.read_payload(target)
        except NotImplementedError as error:
            raise UnsupportedEnvironmentFact("read-only payload source does not support environment facts") from error
        except Exception as error:
            raise EnvironmentObservationAcquisitionError("read-only payload source failed") from error
        return normalize_environment_facts_payload(payload)


class FrappeEnvironmentFactsSource:
    """Concrete source over one fixed, read-only Frappe facts adapter."""

    def __init__(self, client: FrappeEnvironmentFactsClient) -> None:
        self.client = client

    def read_payload(self, target: LockTarget) -> Mapping[str, Any]:
        try:
            payload = self.client.get_environment_facts(target.site)
        except NotImplementedError as error:
            raise UnsupportedEnvironmentFact("Frappe facts endpoint is unavailable") from error
        except Exception as error:
            raise EnvironmentObservationAcquisitionError("Frappe facts adapter failed") from error
        if not isinstance(payload, Mapping):
            raise EnvironmentObservationAcquisitionError("Frappe facts adapter returned a non-object payload")
        return payload


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise EnvironmentObservationAcquisitionError(f"{label} payload keys are not the approved shape")


def _object(value: Any, label: str, expected: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EnvironmentObservationAcquisitionError(f"{label} must be an object")
    _exact_keys(value, expected, label)
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise EnvironmentObservationAcquisitionError(f"{label} must be a list")
    return value


def _tool(value: Any) -> ToolStatus:
    item = _object(value, "tool", {"name", "available", "version"})
    try:
        return ToolStatus(item["name"], item["available"], item["version"])
    except (TypeError, ValueError, KeyError) as error:
        raise EnvironmentObservationAcquisitionError("tool payload is malformed") from error


def _health(value: Any) -> HealthCheck:
    item = _object(value, "health check", {"name", "passed", "detail"})
    try:
        return HealthCheck(item["name"], item["passed"], item["detail"])
    except (TypeError, ValueError, KeyError) as error:
        raise EnvironmentObservationAcquisitionError("health check payload is malformed") from error


def normalize_environment_observation(
    preflight: EnvironmentPreflight,
    ownership: OwnershipObservation,
) -> EnvironmentObservation:
    """Normalize typed inspector outputs without acquiring any environment capability."""

    return EnvironmentObservation(preflight, ownership)


@dataclass(frozen=True)
class EnvironmentPreflightPolicy:
    """Explicit requirements; callers must opt in to each provider tool."""

    required_tools: tuple[str, ...] = ("bench", "frappectl")
    required_health_checks: tuple[str, ...] = ("database", "site")
    minimum_free_bytes: int = 1_073_741_824
    target_app_state: Literal["present", "absent", "either"] = "either"
    required_frappe_major: int = 16

    def __post_init__(self) -> None:
        for label, names in (("required tool", self.required_tools), ("required health check", self.required_health_checks)):
            if not isinstance(names, tuple):
                raise ValueError(f"{label}s must be immutable tuples")
            for name in names:
                _required_text(name, label, _NAME_RE)
            if len(names) != len(set(names)):
                raise ValueError(f"{label}s must be unique")
        if not isinstance(self.minimum_free_bytes, int) or isinstance(self.minimum_free_bytes, bool) or self.minimum_free_bytes < 0:
            raise ValueError("minimum_free_bytes must be a non-negative integer")
        if self.target_app_state not in {"present", "absent", "either"}:
            raise ValueError("target_app_state must be 'present', 'absent', or 'either'")
        if not isinstance(self.required_frappe_major, int) or isinstance(self.required_frappe_major, bool) or self.required_frappe_major < 1:
            raise ValueError("required_frappe_major must be a positive integer")


@dataclass(frozen=True)
class PreflightFailure:
    code: str
    message: str


@dataclass(frozen=True)
class EnvironmentEligibility:
    """Fail-closed result: only an empty failure list permits execution."""

    eligible: bool
    failures: tuple[PreflightFailure, ...]

    @property
    def failure_codes(self) -> tuple[str, ...]:
        return tuple(failure.code for failure in self.failures)


def evaluate_environment_preflight(
    preflight: EnvironmentPreflight,
    *,
    policy: EnvironmentPreflightPolicy = EnvironmentPreflightPolicy(),
    intent: RunIntent | None = None,
) -> EnvironmentEligibility:
    """Evaluate a fully gathered snapshot without executing or inspecting anything.

    Unknown facts never pass: Frappe must be exactly v16, developer mode must
    be true, production designation must be false, every requested tool and
    health check must explicitly pass, and disk state must prove enough writable
    capacity.  When an intent is supplied, all three target fields must match.
    """

    failures: list[PreflightFailure] = []
    if intent is not None:
        expected = (intent.target.bench, intent.target.site, intent.target.app)
        actual = (preflight.identity.bench, preflight.identity.site, preflight.identity.app)
        if actual != expected:
            _fail(failures, "target_identity_mismatch", "observed Bench/site/app does not match the immutable run intent")
    if preflight.frappe.major != policy.required_frappe_major:
        _fail(failures, "unsupported_frappe_major", f"Frappe major must be exactly {policy.required_frappe_major}")
    if preflight.safety.developer_mode is not True:
        _fail(failures, "developer_mode_required", "developer mode must be explicitly enabled")
    if preflight.safety.production_designated is not False:
        _fail(failures, "production_designation_blocked", "production or unknown designation is ineligible")
    app_is_installed = preflight.identity.app in preflight.installed_apps
    if policy.target_app_state == "present" and not app_is_installed:
        _fail(failures, "target_app_not_installed", "the target app is not confirmed in installed_apps")
    if policy.target_app_state == "absent" and app_is_installed:
        _fail(failures, "target_app_conflict", "the target app already exists where a new app is required")
    _check_tools(preflight, policy, failures)
    _check_disk(preflight.disk, policy, failures)
    _check_health(preflight, policy, failures)
    return EnvironmentEligibility(not failures, tuple(failures))


def evaluate_ownership(
    observation: OwnershipObservation,
    *,
    intent: RunIntent | None = None,
) -> OwnershipEligibility:
    """Evaluate inspector-supplied source ownership facts without I/O."""

    failures: list[PreflightFailure] = []
    if intent is not None:
        expected = (intent.target.bench, intent.target.site, intent.target.app)
        actual = (observation.identity.bench, observation.identity.site, observation.identity.app)
        if actual != expected:
            _fail(failures, "ownership_target_mismatch", "ownership observation does not match the immutable run intent")
    if observation.source_clean is not True:
        _fail(failures, "source_not_clean", "target app source must be explicitly clean")
    if observation.ownership_conflict is not False:
        _fail(failures, "ownership_conflict", "app ownership must be explicitly conflict-free")
    return OwnershipEligibility(not failures, tuple(failures))


def _check_tools(preflight: EnvironmentPreflight, policy: EnvironmentPreflightPolicy, failures: list[PreflightFailure]) -> None:
    tools = {tool.name: tool for tool in preflight.tools}
    for name in policy.required_tools:
        if tools.get(name) is None:
            _fail(failures, "required_tool_unobserved", f"required tool {name!r} was not observed")
        elif tools[name].available is not True:
            _fail(failures, "required_tool_unavailable", f"required tool {name!r} is unavailable or unknown")


def _check_disk(disk: DiskHealth, policy: EnvironmentPreflightPolicy, failures: list[PreflightFailure]) -> None:
    if disk.writable is not True:
        _fail(failures, "disk_not_writable", "target disk is not explicitly writable")
    if disk.free_bytes is None or disk.free_bytes < policy.minimum_free_bytes:
        _fail(failures, "insufficient_disk_space", "target disk free capacity is unknown or below policy")


def _check_health(preflight: EnvironmentPreflight, policy: EnvironmentPreflightPolicy, failures: list[PreflightFailure]) -> None:
    checks = {check.name: check for check in preflight.health_checks}
    for name in policy.required_health_checks:
        if checks.get(name) is None:
            _fail(failures, "required_health_check_unobserved", f"required health check {name!r} was not observed")
        elif checks[name].passed is not True:
            _fail(failures, "required_health_check_failed", f"required health check {name!r} failed or is unknown")


def _fail(failures: list[PreflightFailure], code: str, message: str) -> None:
    failures.append(PreflightFailure(code, message))
