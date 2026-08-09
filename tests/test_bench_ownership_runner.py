from __future__ import annotations

import subprocess

import pytest

from frappe_harness.bench_ownership_runner import OwnershipAcquisitionError, OwnershipRunner
from frappe_harness.run_store import LockTarget


def _target():
    return LockTarget("/bench", "dev.local", "task_tracker")


def test_ownership_runner_returns_clean_conflict_free_facts():
    def process(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1 if argv[0] == "test" else 0, b"", b"") if argv[0] == "test" else subprocess.CompletedProcess(argv, 0, b"", b"")

    facts = OwnershipRunner(process).inspect(_target())
    assert facts.source_clean is True
    assert facts.ownership_conflict is False


def test_ownership_runner_detects_generated_paths_without_retaining_output():
    def process(argv, **kwargs):
        if argv[0] == "test" and argv[-1].endswith("/api.py"):
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if argv[0] == "test":
            return subprocess.CompletedProcess(argv, 1, b"", b"")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    facts = OwnershipRunner(process).inspect(_target())
    assert facts.source_clean is True
    assert facts.ownership_conflict is True


def test_ownership_runner_fails_closed_on_git_error():
    def process(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 2, b"secret detail", b"")

    with pytest.raises(OwnershipAcquisitionError, match="non-zero"):
        OwnershipRunner(process).inspect(_target())
