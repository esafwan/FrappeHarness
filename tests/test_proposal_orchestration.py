from __future__ import annotations

import json
from pathlib import Path
import pytest

from frappe_harness.contracts import spec_hash
from frappe_harness.proposal_orchestration import BoundedProposalOrchestrator, ProposalAdmissionError
from frappe_harness.spec_io import load_project_spec


def _raw_fixture():
    return json.loads((Path(__file__).parents[1] / "fixtures" / "task_tracker.json").read_text())


def test_natural_language_extractor_reaches_exact_golden_hash():
    expected = load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")

    class Extractor:
        def extract(self, prompt, *, feedback=()):
            assert "task tracker" in prompt.lower()
            return _raw_fixture()

    result = BoundedProposalOrchestrator(Extractor()).extract(
        "Build the golden Task Tracker.", expected_spec_hash=spec_hash(expected),
    )
    assert result.equivalent
    assert not result.escalated
    assert result.spec is not None
    assert spec_hash(result.spec) == spec_hash(expected)


def test_require_equivalent_is_the_only_admission_bridge():
    expected = load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")

    class Extractor:
        def extract(self, prompt, *, feedback=()):
            return _raw_fixture()

    admitted = BoundedProposalOrchestrator(Extractor()).require_equivalent(
        "build the golden task tracker", expected_spec_hash=spec_hash(expected),
    )
    assert spec_hash(admitted) == spec_hash(expected)

    with pytest.raises(ProposalAdmissionError, match="SHA-256"):
        BoundedProposalOrchestrator(Extractor()).require_equivalent("prompt", expected_spec_hash="bad")


def test_mismatched_golden_proposal_retries_once_then_escalates():
    raw = _raw_fixture()
    raw["name"] = "other_project"

    class Extractor:
        calls = 0
        def extract(self, prompt, *, feedback=()):
            self.calls += 1
            return raw

    expected = load_project_spec(Path(__file__).parents[1] / "fixtures" / "task_tracker.json")
    extractor = Extractor()
    result = BoundedProposalOrchestrator(extractor).extract(
        "Build the golden Task Tracker.", expected_spec_hash=spec_hash(expected),
    )
    assert extractor.calls == 2
    assert result.escalated
    assert not result.equivalent
    assert result.attempts[-1].result.issues[0].code == "golden_spec_mismatch"


def test_validation_only_proposal_is_not_claimed_golden_equivalent():
    class Extractor:
        def extract(self, prompt, *, feedback=()):
            return _raw_fixture()

    result = BoundedProposalOrchestrator(Extractor()).extract("Build a Task Tracker preview.")
    assert result.spec is not None
    assert not result.equivalent
    assert not result.escalated
    assert result.reason == "golden_spec_hash_not_provided"
