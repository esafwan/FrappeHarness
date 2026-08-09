from __future__ import annotations

import pytest

from frappe_harness.frappectl_policy import (
    DocGetRequest,
    DocListRequest,
    DoctypeShowRequest,
    FrappectlPolicyError,
    build_readonly_argv,
)


def test_allowlisted_read_requests_produce_exact_json_argv() -> None:
    assert build_readonly_argv(DoctypeShowRequest("Task")) == (
        "frappectl", "--json", "doctype", "show", "Task"
    )
    assert build_readonly_argv(DocListRequest("Task", limit=5)) == (
        "frappectl", "--json", "doc", "list", "Task", "--limit", "5"
    )
    assert build_readonly_argv(DocGetRequest("Task", "TASK-0001")) == (
        "frappectl", "--json", "doc", "get", "Task", "TASK-0001"
    )
    assert build_readonly_argv(DoctypeShowRequest("Task", raw=True)) == (
        "frappectl", "--json", "doctype", "show", "Task", "--raw"
    )


@pytest.mark.parametrize(
    "candidate",
    [
        DoctypeShowRequest("Task; migrate"),
        DocListRequest("Task", limit=101),
        DocListRequest("Task", limit=True),
        DocGetRequest("Task", "../../site_config.json"),
        DocGetRequest("Task", "--delete"),
        DocGetRequest("Task", "TASK$(whoami)"),
        {"command": "doc delete Task TASK-0001"},
        "frappectl --json doc list Task",
    ],
)
def test_rejects_arbitrary_commands_paths_queries_and_mutations(candidate: object) -> None:
    with pytest.raises(FrappectlPolicyError):
        build_readonly_argv(candidate)  # type: ignore[arg-type]


def test_list_does_not_accept_filters_ordering_or_other_untyped_query_input() -> None:
    with pytest.raises(TypeError):
        DocListRequest("Task", filters="status=Open")  # type: ignore[call-arg]


def test_profile_is_a_validated_local_reference_not_a_url_or_credential() -> None:
    assert build_readonly_argv(DoctypeShowRequest("Task"), profile="harness-live") == (
        "frappectl", "--json", "--site", "harness-live", "doctype", "show", "Task"
    )
    for value in ("https://site.example", "name with spaces", "-switch", "key:secret"):
        with pytest.raises(FrappectlPolicyError):
            build_readonly_argv(DoctypeShowRequest("Task"), profile=value)
