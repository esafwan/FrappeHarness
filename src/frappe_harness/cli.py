"""Safe, direct operator CLI for the early harness vertical slice."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from .compiler import CompilationError, compile_project
from .contracts import spec_hash, validate_project_spec
from .provider_plan import build_metadata_plan, plan_json
from .run_store import LockTarget, RunIntent, RunNotFound, RunState, RunStore, RunStoreError
from .spec_io import SpecInputError, load_project_spec
from .milestone_gates import evaluate_milestone_gate_for_run
from .release_evaluation import candidate_from_normalized_result, resolve_release_evidence
from .evaluation_corpus import load_corpus
from .route_trace import parse_route_trace, replay_next_action
from .stage_flow import Stage, StageFlow, build_approval


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="frappe-harness", description="Constrained Frappe 16 app harness")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="validate a typed JSON project specification")
    validate.add_argument("spec", type=Path)

    compile_command = commands.add_parser("compile", help="render compiler-owned artifacts without a Bench")
    compile_command.add_argument("spec", type=Path)
    compile_command.add_argument("--output", required=True, type=Path)

    plan_command = commands.add_parser("plan", help="produce a typed Frappe-native provider plan")
    plan_command.add_argument("spec", type=Path)
    plan_command.add_argument("--provider", choices=["frappe-native", "commander-spike"], default="frappe-native")

    start = commands.add_parser("run-start", help="create a durable, exclusively locked run")
    _store_args(start)
    start.add_argument("--bench", required=True)
    start.add_argument("--site", required=True)
    start.add_argument("--app", required=True)
    start.add_argument("--provider", choices=["frappe-native", "commander-spike"], required=True)
    start.add_argument("--spec-hash", required=True)
    start.add_argument("--plan-hash", required=True)

    approve = commands.add_parser("approve", help="record a core approval and move a run to approved")
    _store_args(approve)
    approve.add_argument("run_id")
    approve.add_argument("--approver", required=True)
    approve.add_argument("--reason", default="")
    approve.add_argument("--provider", choices=["frappe-native", "commander-spike"], required=True)
    approve.add_argument("--spec-hash", required=True)
    approve.add_argument("--plan-hash", required=True)

    gate_approve = commands.add_parser("gate-approve", help="record an explicit milestone/release gate decision")
    _store_args(gate_approve)
    gate_approve.add_argument("run_id")
    gate_approve.add_argument("--gate", required=True)
    gate_approve.add_argument("--approver", required=True)
    gate_approve.add_argument("--reject", action="store_true")
    gate_approve.add_argument("--provider", choices=["frappe-native", "commander-spike"], required=True)
    gate_approve.add_argument("--spec-hash", required=True)
    gate_approve.add_argument("--plan-hash", required=True)

    gate_evaluate = commands.add_parser("gate-evaluate", help="evaluate a gate from supplied evidence without recording approval")
    _store_args(gate_evaluate)
    gate_evaluate.add_argument("run_id")
    gate_evaluate.add_argument("--gate", required=True)
    gate_evaluate.add_argument("--evidence-json", required=True, help="JSON object mapping evidence names to true/false")
    gate_evaluate.add_argument("--model-thresholds-passed", action="store_true")
    gate_evaluate.add_argument("--corpus-coverage-complete", action="store_true")
    gate_evaluate.add_argument("--unsafe-admissions-zero", action="store_true")
    gate_evaluate.add_argument("--uncontrolled-command-paths-zero", action="store_true")
    gate_evaluate.add_argument("--release-aggregate", type=Path, help="normalized release comparison artifact (required for REL-GATE)")
    gate_evaluate.add_argument("--release-candidate", type=Path, action="append", help="normalized candidate result; repeat for every candidate")

    release_resolve = commands.add_parser("release-resolve", help="derive release facts from normalized candidate evidence")
    release_resolve.add_argument("--aggregate", required=True, type=Path)
    release_resolve.add_argument("--candidate", required=True, type=Path, action="append")
    release_resolve.add_argument("--uncontrolled-command-paths-zero", action="store_true")

    transition = commands.add_parser("run-transition", help="advance a run through its guarded lifecycle")
    _store_args(transition)
    transition.add_argument("run_id")
    # Approval is only recorded through the approval command; it is never a
    # generic, manually selectable lifecycle destination.
    transition.add_argument("state", choices=[state.value for state in RunState if state is not RunState.APPROVED])

    status = commands.add_parser("status", help="show a durable run record")
    _store_args(status)
    status.add_argument("run_id")

    route_status = commands.add_parser("route-status", help="show a redacted persisted assisted-flow trace")
    route_status.add_argument("trace", type=Path)

    stage = commands.add_parser("stage", help="advance the human-approved staged workflow")
    stage.add_argument("state", type=Path, help="stage state JSON; created if absent")
    stage.add_argument("--approve", choices=[s.value for s in Stage])
    stage.add_argument("--approver")
    stage.add_argument("--reason")
    chat = commands.add_parser("stage-chat", help="interactive resumable human-in-loop stage session")
    chat.add_argument("state", type=Path)

    demo = commands.add_parser("demo-run", help="exercise the complete local lifecycle with no Bench or network")
    _store_args(demo)
    return parser


def _store_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store", type=Path, default=Path(".frappe-harness/runs.sqlite3"))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            return _validate(args.spec)
        if args.command == "compile":
            return _compile(args.spec, args.output)
        if args.command == "plan":
            spec = load_project_spec(args.spec)
            plan = build_metadata_plan(spec, provider=args.provider)
            print(plan_json(plan), end="")
            return 0
        if args.command == "run-start":
            store = RunStore(args.store)
            intent = RunIntent(LockTarget(args.bench, args.site, args.app), args.provider, args.spec_hash, args.plan_hash)
            run = store.create_run(intent)
            _emit({"run_id": run.id, "state": run.state.value, "target": run.target.__dict__})
            return 0
        if args.command == "release-resolve":
            aggregate = json.loads(args.aggregate.read_text(encoding="utf-8"))
            corpus_id = aggregate.get("corpus_id", "")
            corpus = load_corpus()
            if corpus.corpus_id != corpus_id:
                raise ValueError("release aggregate is not bound to the bundled V1 corpus")
            candidates = [
                candidate_from_normalized_result(
                    json.loads(path.read_text(encoding="utf-8")),
                    expected_corpus_id=corpus_id,
                    expected_case_ids=(case.id for case in corpus.cases),
                )
                for path in args.candidate
            ]
            resolved = resolve_release_evidence(
                aggregate,
                candidates,
                )
            _emit(resolved.__dict__)
            return 0 if resolved.comparison_valid and not resolved.blockers else 2
        if args.command == "route-status":
            trace = parse_route_trace(json.loads(args.trace.read_text(encoding="utf-8")))
            _emit({
                "request_id": trace.request_id,
                "decision_id": trace.decision_id,
                "route": trace.route.value,
                "policy_approved": trace.policy_approved,
                "hard_stop": trace.hard_stop,
                "next_action": replay_next_action(trace),
                "trace_digest": trace.trace_digest,
            })
            return 0
        if args.command == "stage":
            from .stage_flow import StageFlowError, StageRecord
            raw = json.loads(args.state.read_text()) if args.state.exists() else {"records": []}
            records = tuple(StageRecord(**item) for item in raw.get("records", []))
            flow = StageFlow(records)
            if args.approve:
                if not args.approver or not args.reason:
                    raise ValueError("stage approval requires --approver and --reason")
                flow = flow.approve(build_approval(Stage(args.approve), args.approver, args.reason))
                args.state.write_text(json.dumps(flow.to_record(), sort_keys=True, indent=2) + "\n")
            _emit({"current": flow.current.value if flow.current else None, "complete": flow.complete, "flow_digest": flow.digest, "records": [r.__dict__ for r in flow.records]})
            return 0
        if args.command == "stage-chat":
            from .stage_chat import run_stage_chat
            return run_stage_chat(args.state)
        store = RunStore(args.store)
        if args.command == "approve":
            run = store.get_run(args.run_id)
            intent = RunIntent(run.target, args.provider, args.spec_hash, args.plan_hash)
            approval = store.record_approval(args.run_id, approver=args.approver, approved=True, intent=intent, rationale=args.reason or None)
            run = store.get_run(args.run_id)
            if run.state is RunState.DRAFT:
                run = store.transition(args.run_id, RunState.PENDING_APPROVAL)
            if run.state is RunState.PENDING_APPROVAL:
                run = store.transition(args.run_id, RunState.APPROVED)
            _emit({"approval_id": approval.id, "run_id": run.id, "state": run.state.value})
            return 0
        if args.command == "gate-approve":
            run = store.get_run(args.run_id)
            intent = RunIntent(run.target, args.provider, args.spec_hash, args.plan_hash)
            approval = store.record_milestone_gate_approval(
                args.run_id, gate=args.gate, approver=args.approver,
                approved=not args.reject, intent=intent,
            )
            _emit({
                "gate_approval_id": approval.id,
                "run_id": run.id,
                "gate": approval.gate,
                "decision": approval.decision,
                "approver": approval.approver,
                "target": run.target.__dict__,
                "provider": approval.intent.provider,
                "spec_hash": approval.intent.spec_hash,
                "plan_hash": approval.intent.plan_hash,
            })
            return 0
        if args.command == "gate-evaluate":
            try:
                evidence = json.loads(args.evidence_json)
            except json.JSONDecodeError as error:
                raise ValueError("--evidence-json must be valid JSON") from error
            if not isinstance(evidence, dict) or any(not isinstance(key, str) or not isinstance(value, (bool, str)) for key, value in evidence.items()):
                raise ValueError("--evidence-json must be an object of boolean diagnostic values or durable evidence references")
            if any(isinstance(value, str) for value in evidence.values()):
                if not all(isinstance(value, str) for value in evidence.values()):
                    raise ValueError("--evidence-json cannot mix boolean diagnostics and durable references")
                evidence_refs, evidence = evidence, None
            else:
                evidence_refs = None
            release_resolution = None
            if args.gate == "REL-GATE":
                if args.release_aggregate is not None or args.release_candidate:
                    if args.release_aggregate is None or not args.release_candidate:
                        raise ValueError("REL-GATE requires --release-aggregate and at least one --release-candidate")
                    aggregate = json.loads(args.release_aggregate.read_text())
                    if not isinstance(aggregate, dict) or not isinstance(aggregate.get("corpus_id"), str):
                        raise ValueError("release aggregate must contain a corpus_id")
                    corpus = load_corpus()
                    if corpus.corpus_id != aggregate["corpus_id"]:
                        raise ValueError("release aggregate is not bound to the bundled V1 corpus")
                    candidates = [
                        candidate_from_normalized_result(
                            json.loads(path.read_text()),
                            expected_corpus_id=aggregate["corpus_id"],
                            expected_case_ids=(case.id for case in corpus.cases),
                        )
                        for path in args.release_candidate
                    ]
                    release_resolution = resolve_release_evidence(
                        aggregate,
                        candidates,
                        uncontrolled_command_paths_zero=args.uncontrolled_command_paths_zero,
                    )
            decision = evaluate_milestone_gate_for_run(
                store, args.run_id, args.gate, evidence=evidence, evidence_refs=evidence_refs,
                model_thresholds_passed=args.model_thresholds_passed,
                corpus_coverage_complete=args.corpus_coverage_complete,
                unsafe_admissions_zero=args.unsafe_admissions_zero,
                uncontrolled_command_paths_zero=args.uncontrolled_command_paths_zero,
                release_resolution=release_resolution,
            )
            _emit({"run_id": args.run_id, "gate": decision.gate, "passed": decision.passed, "failures": decision.failures})
            return 0
        if args.command == "run-transition":
            run = store.transition(args.run_id, RunState(args.state))
            _emit({"run_id": run.id, "state": run.state.value})
            return 0
        if args.command == "status":
            run = store.get_run(args.run_id)
            _emit({"run_id": run.id, "state": run.state.value, "target": run.target.__dict__, "metadata": dict(run.metadata), "created_at": run.created_at, "updated_at": run.updated_at})
            return 0
        if args.command == "demo-run":
            from .local_demo import run_local_demo
            _emit(run_local_demo(args.store).__dict__)
            return 0
    except (SpecInputError, CompilationError, RunStoreError, ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    raise AssertionError(f"unknown command {args.command}")


def _validate(path: Path) -> int:
    spec = load_project_spec(path)
    report = validate_project_spec(spec)
    _emit({"valid": report.valid, "spec_hash": spec_hash(spec), "issues": [issue.__dict__ | {"severity": issue.severity.value} for issue in report.issues]})
    return 0 if report.valid else 2


def _compile(spec_path: Path, output: Path) -> int:
    spec = load_project_spec(spec_path)
    report = validate_project_spec(spec)
    if not report.valid:
        _emit({"valid": False, "issues": [issue.__dict__ | {"severity": issue.severity.value} for issue in report.issues]})
        return 2
    result = compile_project(spec)
    if output.exists() and any(output.iterdir()):
        raise CompilationError(f"refusing to write into non-empty output directory: {output}")
    output.mkdir(parents=True, exist_ok=True)
    for relative_path, content in result.artifacts.items():
        target = output / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    _emit({"valid": True, "spec_hash": result.spec_hash, "output": str(output), "artifacts": sorted(result.artifacts)})
    return 0


def _emit(value: object) -> None:
    print(json.dumps(value, sort_keys=True, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
