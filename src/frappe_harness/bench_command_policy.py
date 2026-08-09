"""Pure, typed command construction for the narrow Frappe Bench allowlist.

This is deliberately a policy module, not a Bench adapter.  It neither
inspects nor executes anything: an inspection adapter creates the immutable
target and an execution adapter may pass the resulting argv to a subprocess
with ``shell=False``.  There is intentionally no raw-command escape hatch.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TypeAlias


class BenchCommandPolicyError(ValueError):
    """A requested Bench operation is outside the typed allowlist."""


_SITE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_APP_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_FORBIDDEN_PATH_CHARS = frozenset("*?[]{}$;|&`'\"<>()\\")


def _bench_path(value: object) -> str:
    """Accept one already-inspected, canonical absolute POSIX Bench path.

    No filesystem access is performed here.  Rejecting non-canonical spellings
    (including ``.`` and ``..`` components) ensures an executor cannot later
    resolve this target to somewhere other than the inspected Bench.
    """

    if not isinstance(value, str) or not value or value != value.strip():
        raise BenchCommandPolicyError("bench_path must be a non-blank trimmed string")
    if not value.startswith("/") or value == "/" or value.endswith("/"):
        raise BenchCommandPolicyError("bench_path must be a canonical absolute non-root path")
    if any(character in _FORBIDDEN_PATH_CHARS or character.isspace() for character in value):
        raise BenchCommandPolicyError("bench_path contains unsupported characters")
    components = value.split("/")
    if any(component in {"", ".", ".."} for component in components[1:]):
        raise BenchCommandPolicyError("bench_path must not contain traversal or empty components")
    return value


def _identifier(value: object, *, label: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise BenchCommandPolicyError(f"{label} must be an allowlisted identifier")
    return value


@dataclass(frozen=True)
class InspectedBenchTarget:
    """The exact Bench/site/app identity produced by a successful inspection."""

    bench_path: str
    site_name: str
    app_slug: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "bench_path", _bench_path(self.bench_path))
        object.__setattr__(self, "site_name", _identifier(self.site_name, label="site_name", pattern=_SITE_RE))
        object.__setattr__(self, "app_slug", _identifier(self.app_slug, label="app_slug", pattern=_APP_RE))


@dataclass(frozen=True)
class InspectBenchVersion:
    target: InspectedBenchTarget


@dataclass(frozen=True)
class SiteListApps:
    target: InspectedBenchTarget


@dataclass(frozen=True)
class SiteShowConfig:
    target: InspectedBenchTarget


@dataclass(frozen=True)
class NewAppScaffold:
    target: InspectedBenchTarget


@dataclass(frozen=True)
class SiteInstallApp:
    target: InspectedBenchTarget


@dataclass(frozen=True)
class SiteBackup:
    target: InspectedBenchTarget


@dataclass(frozen=True)
class SiteMigrate:
    target: InspectedBenchTarget


@dataclass(frozen=True)
class BuildApp:
    target: InspectedBenchTarget


@dataclass(frozen=True)
class ClearSiteCache:
    target: InspectedBenchTarget


@dataclass(frozen=True)
class RunAppTests:
    target: InspectedBenchTarget


BenchOperation: TypeAlias = (
    InspectBenchVersion
    | SiteListApps
    | SiteShowConfig
    | NewAppScaffold
    | SiteInstallApp
    | SiteBackup
    | SiteMigrate
    | BuildApp
    | ClearSiteCache
    | RunAppTests
)


def build_bench_argv(operation: BenchOperation, *, inspected_target: InspectedBenchTarget) -> tuple[str, ...]:
    """Build one exact Bench argv for a confirmed target and operation.

    The caller must supply the same immutable inspection result that admitted
    the run.  All site operations use the standard ``bench --site`` form;
    nothing in this module accepts a shell snippet, a generic subcommand, or
    extra flags.
    """

    if not isinstance(inspected_target, InspectedBenchTarget):
        raise BenchCommandPolicyError("inspected_target must be an InspectedBenchTarget")
    if not isinstance(operation, _OPERATION_TYPES):
        raise BenchCommandPolicyError("only typed allowlisted Bench operations are allowed")
    if operation.target != inspected_target:
        raise BenchCommandPolicyError("operation target does not match the inspected target")

    site_prefix = ("bench", "--site", inspected_target.site_name)
    if isinstance(operation, InspectBenchVersion):
        return ("bench", "version")
    if isinstance(operation, SiteListApps):
        return site_prefix + ("list-apps", "--format", "json")
    if isinstance(operation, SiteShowConfig):
        return site_prefix + ("show-config", "-f", "json")
    if isinstance(operation, NewAppScaffold):
        return ("bench", "new-app", inspected_target.app_slug)
    if isinstance(operation, SiteInstallApp):
        return site_prefix + ("install-app", inspected_target.app_slug)
    if isinstance(operation, SiteBackup):
        return site_prefix + ("backup",)
    if isinstance(operation, SiteMigrate):
        return site_prefix + ("migrate",)
    if isinstance(operation, BuildApp):
        return ("bench", "build", "--app", inspected_target.app_slug)
    if isinstance(operation, ClearSiteCache):
        return site_prefix + ("clear-cache",)
    if isinstance(operation, RunAppTests):
        return site_prefix + ("run-tests", "--app", inspected_target.app_slug)
    raise BenchCommandPolicyError("only typed allowlisted Bench operations are allowed")


_OPERATION_TYPES = (
    InspectBenchVersion,
    SiteListApps,
    SiteShowConfig,
    NewAppScaffold,
    SiteInstallApp,
    SiteBackup,
    SiteMigrate,
    BuildApp,
    ClearSiteCache,
    RunAppTests,
)
