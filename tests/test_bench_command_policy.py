from __future__ import annotations

from dataclasses import replace

import pytest

from frappe_harness.bench_command_policy import (
    BenchCommandPolicyError,
    BuildApp,
    ClearSiteCache,
    InspectBenchVersion,
    InspectedBenchTarget,
    SiteListApps,
    SiteShowConfig,
    NewAppScaffold,
    RunAppTests,
    SiteBackup,
    SiteInstallApp,
    SiteMigrate,
    build_bench_argv,
)


@pytest.fixture
def target() -> InspectedBenchTarget:
    return InspectedBenchTarget("/srv/frappe/bench", "harness.local", "task_tracker")


@pytest.mark.parametrize(
    ("operation_type", "expected"),
    [
        (InspectBenchVersion, ("bench", "version")),
        (SiteListApps, ("bench", "--site", "harness.local", "list-apps", "--format", "json")),
        (SiteShowConfig, ("bench", "--site", "harness.local", "show-config", "-f", "json")),
        (NewAppScaffold, ("bench", "new-app", "task_tracker")),
        (SiteInstallApp, ("bench", "--site", "harness.local", "install-app", "task_tracker")),
        (SiteBackup, ("bench", "--site", "harness.local", "backup")),
        (SiteMigrate, ("bench", "--site", "harness.local", "migrate")),
        (BuildApp, ("bench", "build", "--app", "task_tracker")),
        (ClearSiteCache, ("bench", "--site", "harness.local", "clear-cache")),
        (RunAppTests, ("bench", "--site", "harness.local", "run-tests", "--app", "task_tracker")),
    ],
)
def test_allowlisted_typed_operations_have_exact_standard_bench_argv(target, operation_type, expected) -> None:
    assert build_bench_argv(operation_type(target), inspected_target=target) == expected


@pytest.mark.parametrize(
    "bench_path",
    [
        "relative/bench",
        "/",
        "/srv/../bench",
        "/srv//bench",
        "/srv/bench/",
        "/srv/$BENCH",
        "/srv/bench*",
        "/srv/bench; migrate",
        "/srv/bench$(whoami)",
        "/srv/bench path",
    ],
)
def test_target_rejects_noncanonical_paths_traversal_globs_shell_and_environment_syntax(bench_path: str) -> None:
    with pytest.raises(BenchCommandPolicyError):
        InspectedBenchTarget(bench_path, "harness.local", "task_tracker")


@pytest.mark.parametrize("site_name", ["--site", "site;backup", "site name", "site/*"])
def test_target_rejects_site_option_injection_and_globs(site_name: str) -> None:
    with pytest.raises(BenchCommandPolicyError):
        InspectedBenchTarget("/srv/bench", site_name, "task_tracker")


@pytest.mark.parametrize("app_slug", ["--app", "TaskTracker", "task-tracker", "task_tracker;rm", "task*tracker"])
def test_target_rejects_app_slug_option_injection_and_non_allowlisted_identifiers(app_slug: str) -> None:
    with pytest.raises(BenchCommandPolicyError):
        InspectedBenchTarget("/srv/bench", "harness.local", app_slug)


def test_operation_must_bind_to_the_exact_inspected_target(target: InspectedBenchTarget) -> None:
    other_target = replace(target, site_name="other.local")
    with pytest.raises(BenchCommandPolicyError, match="does not match"):
        build_bench_argv(SiteMigrate(other_target), inspected_target=target)


@pytest.mark.parametrize("candidate", [{"command": "bench migrate"}, "bench migrate", object()])
def test_policy_has_no_generic_command_or_untyped_escape_hatch(target: InspectedBenchTarget, candidate: object) -> None:
    with pytest.raises(BenchCommandPolicyError):
        build_bench_argv(candidate, inspected_target=target)  # type: ignore[arg-type]


def test_typed_contracts_are_immutable(target: InspectedBenchTarget) -> None:
    with pytest.raises(AttributeError):
        target.app_slug = "other"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        SiteBackup(target).target = target  # type: ignore[misc]
