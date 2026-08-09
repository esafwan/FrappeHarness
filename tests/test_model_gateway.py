import dataclasses
import inspect
import typing

import pytest

import frappe_harness.model_gateway as gateway
from frappe_harness.model_gateway import (
    Clarification,
    FindingSeverity,
    ModelGateway,
    ProposalItem,
    ProposalPayload,
    ProposalReview,
    ValidationFinding,
    validate_review,
)


def _payload() -> ProposalPayload:
    return ProposalPayload("task_tracker", (ProposalItem("task_status", "Task needs a status."),))


def test_gateway_contract_is_typed_data_in_and_validation_or_clarification_out_only():
    assert issubclass(ModelGateway, typing.Protocol)
    assert list(inspect.signature(ModelGateway.review).parameters) == ["self", "payload", "feedback"]
    payload = _payload()
    review = ProposalReview(
        payload.proposal_id,
        (ValidationFinding(FindingSeverity.WARNING, "ambiguous", "task_status", "Choose statuses."),),
        (Clarification("task_status", "Which status is the default?"),),
    )
    assert validate_review(payload, review) is review
    assert review.accepted


def test_gateway_rejects_review_not_bound_to_its_typed_payload():
    payload = _payload()
    with pytest.raises(ValueError, match="does not match"):
        validate_review(payload, ProposalReview("other"))
    with pytest.raises(ValueError, match="unknown proposal"):
        validate_review(
            payload,
            ProposalReview(payload.proposal_id, clarifications=(Clarification("other", "Why?"),)),
        )


def test_capability_boundary_exposes_no_execution_or_secret_handles():
    forbidden = {
        "shell", "command", "argv", "subprocess", "process", "filesystem", "file", "path",
        "database", "db", "network", "http", "client", "site", "bench", "git", "repo",
        "credential", "secret", "token", "password", "api_key", "tool", "capability",
    }
    contracts = (ProposalItem, ProposalPayload, ValidationFinding, Clarification, ProposalReview)
    for contract in contracts:
        names = {field.name.lower() for field in dataclasses.fields(contract)}
        assert not names & forbidden, f"{contract.__name__} leaked {names & forbidden}"
        hints = typing.get_type_hints(contract)
        assert all("Callable" not in str(hint) for hint in hints.values())

    source = inspect.getsource(gateway).lower()
    assert "import subprocess" not in source
    assert "import os" not in source
    assert "import pathlib" not in source
    assert "import sqlite" not in source
    assert "import requests" not in source


def test_payload_and_review_are_immutable_and_do_not_accept_untyped_collections():
    item = ProposalItem("status", "Specify task status.")
    with pytest.raises(ValueError, match="immutable tuple"):
        ProposalPayload("task", [item])  # type: ignore[arg-type]
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.text = "change"  # type: ignore[misc]
    with pytest.raises(ValueError, match="immutable tuples"):
        ProposalReview("task", findings=[])  # type: ignore[arg-type]
