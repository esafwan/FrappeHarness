"""Normalize a deliberately small set of read-only ``frappectl`` payloads.

The command policy deliberately does not expose generic query or method
operations.  Consequently this module accepts *already retrieved* JSON only
at typed entry points: a DocType show result, an installed-apps result, a
Frappe version result, and a health result.  It never invokes ``frappectl``
and it does not turn arbitrary JSON into an environment attestation.

``FrappectlPreflightFacts`` is a transport-neutral intermediate value.  The
environment adapter must still bind it to the selected Bench/site and collect
facts which are not available from the REST client (for example disk space).
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

from .environment_preflight import FrappeRuntime, HealthCheck


class FrappectlPayloadError(ValueError):
    """A read payload cannot safely be interpreted as its claimed fact."""


_VERSION = re.compile(r"^v?(?P<major>[0-9]+)(?:\.[0-9]+){0,3}(?:[-+][0-9A-Za-z.-]+)?$")


@dataclass(frozen=True)
class FrappectlPreflightFacts:
    """Facts available from fixed read payloads, not a complete preflight.

    ``doctype_names`` confirms only that those named DocTypes were returned by
    a typed ``doctype show`` operation.  It is intentionally not used as proof
    of ownership, permissions, an app install, or a mutation capability.
    """

    frappe: FrappeRuntime
    installed_apps: tuple[str, ...]
    health_check: HealthCheck
    doctype_names: tuple[str, ...] = ()


def normalize_preflight_facts(
    *,
    version_payload: object,
    apps_payload: object,
    health_payload: object,
    health_check_name: str = "site",
    doctype_payloads: Mapping[str, object] | None = None,
) -> FrappectlPreflightFacts:
    """Build facts only if every required typed payload is unambiguous.

    The function is fail-closed: a missing envelope, a false/unknown health
    state, an empty app list, or a DocType payload whose declared name does not
    match its requested key raises :class:`FrappectlPayloadError`.
    """

    frappe = normalize_frappe_runtime_payload(version_payload)
    apps = normalize_installed_apps_payload(apps_payload)
    health_check = normalize_health_check_payload(health_payload, name=health_check_name)
    names = tuple(
        normalize_doctype_show_payload(payload, expected_name=name)["name"]
        for name, payload in (doctype_payloads or {}).items()
    )
    return FrappectlPreflightFacts(
        frappe=frappe,
        installed_apps=apps,
        health_check=health_check,
        doctype_names=names,
    )


def normalize_doctype_show_payload(payload: object, *, expected_name: str) -> Mapping[str, Any]:
    """Return one normalized DocType metadata object from ``doctype show``.

    Only the standard direct object or its ``data`` envelope are accepted.
    Requiring the metadata's own ``name`` prevents a response for one DocType
    from being accidentally treated as evidence for another.
    """

    if not isinstance(expected_name, str) or not expected_name.strip():
        raise FrappectlPayloadError("expected DocType name must be non-blank")
    data = _data_object(payload, label="DocType")
    name = data.get("name")
    if name != expected_name:
        raise FrappectlPayloadError("DocType payload name does not match requested DocType")
    for collection in ("fields", "permissions"):
        value = data.get(collection, ())
        if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
            raise FrappectlPayloadError(f"DocType payload {collection} must be a list of objects")
    return data


def normalize_version_payload(payload: object) -> tuple[str, int]:
    """Extract a semver-like Frappe version from a named version field."""

    data = _data_object(payload, label="version")
    values = [data[key] for key in ("frappe_version", "version") if key in data]
    if len(values) != 1 or not isinstance(values[0], str):
        raise FrappectlPayloadError("version payload must contain exactly one named Frappe version")
    version = values[0].strip()
    match = _VERSION.fullmatch(version)
    if not match:
        raise FrappectlPayloadError("Frappe version is not a supported version string")
    return version, int(match.group("major"))


def normalize_frappe_runtime_payload(payload: object) -> FrappeRuntime:
    """Convert a typed version response directly to the preflight contract."""

    version, major = normalize_version_payload(payload)
    return FrappeRuntime(major=major, version=version)


def normalize_installed_apps_payload(payload: object) -> tuple[str, ...]:
    """Extract a non-empty, duplicate-free installed app list.

    ``frappectl``/Frappe deployments have used both ``apps`` and
    ``installed_apps`` naming, so those two explicit representations are
    supported.  No arbitrary list-shaped payload is accepted.
    """

    data = _data_object(payload, label="installed apps")
    values = [data[key] for key in ("installed_apps", "apps") if key in data]
    if len(values) != 1 or not isinstance(values[0], list):
        raise FrappectlPayloadError("installed-apps payload must contain exactly one named app list")
    apps = tuple(values[0])
    if not apps or any(not isinstance(app, str) or not app.strip() for app in apps):
        raise FrappectlPayloadError("installed app names must be non-blank strings")
    if len(set(apps)) != len(apps):
        raise FrappectlPayloadError("installed app names must not be duplicated")
    return apps


def normalize_health_payload(payload: object) -> bool:
    """Accept only an explicit healthy boolean or an explicit healthy status."""

    data = _data_object(payload, label="health")
    if data.get("healthy") is True and "status" not in data:
        return True
    if data.get("status") in {"ok", "healthy"} and "healthy" not in data:
        return True
    raise FrappectlPayloadError("health payload does not explicitly attest a healthy state")


def normalize_health_check_payload(payload: object, *, name: str = "site") -> HealthCheck:
    """Convert an explicit healthy response to one immutable health check."""

    return HealthCheck(name=name, passed=normalize_health_payload(payload))


def _data_object(payload: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise FrappectlPayloadError(f"{label} payload must be an object")
    if "data" in payload:
        data = payload["data"]
        if not isinstance(data, Mapping):
            raise FrappectlPayloadError(f"{label} payload data envelope must be an object")
        return data
    return payload
