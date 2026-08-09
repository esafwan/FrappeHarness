"""Pure, fail-closed milestone and release gate decisions."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from .run_store import RunStore
    from .release_evaluation import ReleaseEvidenceResolution


GATES = (
    "CMP-GATE", "BEX-GATE", "VAL-GATE", "VER-GATE", "FE-GATE",
    "ORC-GATE", "LLM-GATE", "PRV-GATE", "PI-GATE", "MOD-GATE", "REL-GATE",
)


@dataclass(frozen=True)
class MilestoneGateInput:
    """Evidence and explicit approval attestations supplied by governance."""

    evidence: Mapping[str, bool]
    approvals: Mapping[str, bool]
    model_thresholds_passed: bool = False
    corpus_coverage_complete: bool = False
    unsafe_admissions_zero: bool = False
    uncontrolled_command_paths_zero: bool = False


@dataclass(frozen=True)
class MilestoneGateDecision:
    gate: str
    passed: bool
    failures: tuple[str, ...]


_EVIDENCE: Mapping[str, tuple[str, ...]] = {
    "CMP-GATE": ("compiler_golden",),
    "BEX-GATE": ("controlled_execution",),
    "VAL-GATE": ("safety_validation",),
    "VER-GATE": ("backend_verification",),
    "FE-GATE": ("frontend_verification",),
    "ORC-GATE": ("lifecycle_receipt",),
    "LLM-GATE": ("bounded_model_equivalence",),
    "PRV-GATE": ("provider_compatibility",),
    "PI-GATE": ("pi_compatibility",),
    "MOD-GATE": ("safe_modification", "destructive_no_mutation"),
    "REL-GATE": ("release_pack",),
}


def evaluate_milestone_gate(gate: str, facts: MilestoneGateInput) -> MilestoneGateDecision:
    """Evaluate one gate; no implicit approval is inferred from evidence."""

    if gate not in GATES:
        raise ValueError("unknown milestone gate")
    if not isinstance(facts, MilestoneGateInput):
        raise TypeError("gate facts must be MilestoneGateInput")
    failures = [f"missing_evidence:{name}" for name in _EVIDENCE[gate] if facts.evidence.get(name) is not True]
    if facts.approvals.get(gate) is not True:
        failures.append("explicit_approval_required")
    if gate == "LLM-GATE" and facts.model_thresholds_passed is not True:
        failures.append("model_thresholds_required")
    if gate == "REL-GATE":
        if facts.model_thresholds_passed is not True:
            failures.append("model_thresholds_required")
        if facts.corpus_coverage_complete is not True:
            failures.append("corpus_coverage_must_be_complete")
        if facts.unsafe_admissions_zero is not True:
            failures.append("unsafe_admissions_must_be_zero")
        if facts.uncontrolled_command_paths_zero is not True:
            failures.append("uncontrolled_command_paths_must_be_zero")
    return MilestoneGateDecision(gate, not failures, tuple(failures))


def evaluate_all_milestone_gates(facts: MilestoneGateInput) -> tuple[MilestoneGateDecision, ...]:
    """Return deterministic decisions for every gate without short-circuiting."""

    return tuple(evaluate_milestone_gate(gate, facts) for gate in GATES)


def evaluate_milestone_gate_for_run(
    store: "RunStore", run_id: str, gate: str, *, evidence: Mapping[str, bool] | None = None,
    evidence_refs: Mapping[str, str] | None = None,
    model_thresholds_passed: bool = False, corpus_coverage_complete: bool = False,
    unsafe_admissions_zero: bool = False, uncontrolled_command_paths_zero: bool = False,
    release_resolution: "ReleaseEvidenceResolution | None" = None,
) -> MilestoneGateDecision:
    """Load exact durable approval for a run, then delegate to pure evaluation."""

    run = store.get_run(run_id)
    if evidence_refs is None:
        # Boolean facts are useful for pure diagnostics, but cannot attest to a
        # run-backed gate. Durable evidence references are mandatory here.
        resolved_evidence = {key: False for key in _EVIDENCE[gate]}
    else:
        required = set(_EVIDENCE[gate])
        if set(evidence_refs) != required or any(not isinstance(value, str) or not value.strip() for value in evidence_refs.values()):
            raise ValueError("run-backed gate evidence_refs must contain exactly the required durable references")
        resolved_evidence: dict[str, bool] = {}
        for key, reference in evidence_refs.items():
            match = re.fullmatch(r"run:(?P<run>[^:]+):evidence:(?P<id>[0-9a-f-]+)", reference)
            if match is None or match.group("run") != run_id:
                raise ValueError(f"{key} evidence reference is not bound to this run")
            record = store.get_lifecycle_evidence(match.group("id"))
            if record.run_id != run_id or record.reference != reference:
                raise ValueError(f"{key} evidence reference does not resolve to this run")
            # The lifecycle emits a typed ``ready_receipt`` as the terminal
            # business receipt; it is the durable lifecycle receipt required
            # by ORC-GATE even though its storage kind is more specific.
            aliases = {
                "lifecycle_receipt": {"ready_receipt"},
                "destructive_no_mutation": {"modification_destructive_no_mutation"},
            }
            if record.kind != key and record.kind not in aliases.get(key, set()):
                raise ValueError(f"{key} evidence reference resolves to unrelated kind {record.kind}")
            resolved_evidence[key] = True
    approvals_for_gate = store.list_milestone_gate_approvals(run_id, gate=gate)
    latest = approvals_for_gate[-1] if approvals_for_gate else None
    approvals = {
        gate: latest is not None and latest.decision == "approved" and latest.intent == run.intent,
    }
    if gate == "REL-GATE":
        # Release facts must come from normalized comparison evidence.  The
        # scalar arguments remain available for diagnostics on other gates but
        # can never attest a release decision by themselves.
        if release_resolution is None:
            model_thresholds_passed = False
            corpus_coverage_complete = False
            unsafe_admissions_zero = False
            uncontrolled_command_paths_zero = False
        else:
            model_thresholds_passed = release_resolution.model_thresholds_passed
            corpus_coverage_complete = release_resolution.corpus_coverage_complete
            unsafe_admissions_zero = release_resolution.unsafe_admissions_zero
            uncontrolled_command_paths_zero = release_resolution.uncontrolled_command_paths_zero
    return evaluate_milestone_gate(
        gate,
        MilestoneGateInput(
            evidence=resolved_evidence,
            approvals=approvals,
            model_thresholds_passed=model_thresholds_passed,
            corpus_coverage_complete=corpus_coverage_complete,
            unsafe_admissions_zero=unsafe_admissions_zero,
            uncontrolled_command_paths_zero=uncontrolled_command_paths_zero,
        ),
    )
