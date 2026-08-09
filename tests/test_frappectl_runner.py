from __future__ import annotations

import hashlib
import subprocess

import pytest

from frappe_harness.frappectl_runner import (
    FrappectlRunner,
    FrappectlToolUnavailable,
    ReadOnlyCommandError,
    ReadOnlyFrappectlCommand,
)


def test_runner_uses_tokenized_subprocess_and_returns_json_on_success(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, b'{"data": [1]}', b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = FrappectlRunner().run(ReadOnlyFrappectlCommand(("frappectl", "--json", "-s", "prod", "doctype", "show", "ToDo")))

    assert seen["argv"] == ("frappectl", "--json", "-s", "prod", "doctype", "show", "ToDo")
    assert seen["kwargs"]["shell"] is False
    assert result.payload == {"data": [1]}
    assert result.receipt.stdout_sha256 == hashlib.sha256(b'{"data": [1]}').hexdigest()
    assert not hasattr(result.receipt, "stdout")


def test_runner_accepts_only_the_typed_doctype_raw_suffix():
    ReadOnlyFrappectlCommand(("frappectl", "--json", "doctype", "show", "Task", "--raw"))
    with pytest.raises(ReadOnlyCommandError):
        ReadOnlyFrappectlCommand(("frappectl", "--json", "doctype", "show", "Task", "--debug"))


def test_nonzero_exit_never_parses_json(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, b"not-json", b"secret diagnostic")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = FrappectlRunner().run(ReadOnlyFrappectlCommand(("frappectl", "--json", "doc", "list", "ToDo")))

    assert result.payload is None
    assert result.receipt.exit_code == 1
    assert result.receipt.stderr_bytes == len(b"secret diagnostic")
    assert result.receipt.stderr_sha256 == hashlib.sha256(b"secret diagnostic").hexdigest()


def test_missing_frappectl_is_a_typed_unavailable_tool_blocker(monkeypatch):
    def fake_run(*_args, **_kwargs):
        raise FileNotFoundError("frappectl")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(FrappectlToolUnavailable, match="pinned read-only verifier"):
        FrappectlRunner().run(ReadOnlyFrappectlCommand(("frappectl", "--json", "doctype", "show", "Task")))


@pytest.mark.parametrize(
    "argv",
    [
        ("frappectl", "doc", "delete", "ToDo", "x"),
        ("frappectl", "api", "method/frappe.client.get_count"),
        ("frappectl", "query", "select 1"),
        ("frappectl", "--writable", "doc", "list", "ToDo"),
        ("bash", "-c", "frappectl doc list ToDo"),
    ],
)
def test_mutating_or_escape_hatch_commands_are_rejected(argv):
    with pytest.raises(ReadOnlyCommandError):
        ReadOnlyFrappectlCommand(argv)
