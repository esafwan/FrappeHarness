"""Small interactive terminal MVP for the human-approved stage engine."""
from __future__ import annotations
import hashlib, json
from pathlib import Path
from .stage_flow import Stage, StageFlow, StageRecord, build_approval, StageFlowError

def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()

def run_stage_chat(state_path: Path, *, input_fn=input, output_fn=print) -> int:
    """Run a resumable, model-agnostic stage conversation.

    Free-form messages are used for the current session only; the state file
    retains only their digest and the normalized stage flow.
    """
    raw = json.loads(state_path.read_text()) if state_path.exists() else {"records": [], "message_digests": []}
    flow = StageFlow(tuple(StageRecord(**item) for item in raw.get("records", [])))
    digests = list(raw.get("message_digests", []))
    output_fn("Frappe Harness staged chat. Commands: /approve, /reject, /back STAGE, /lock STAGE, /show, /quit")
    while True:
        current = flow.current.value if flow.current else "COMPLETE"
        output_fn(f"[{current}] Describe or review the proposal.")
        try: text = input_fn("> ")
        except EOFError: text = "/quit"
        if text.startswith("/"):
            parts = text.split(maxsplit=2); command = parts[0].lower()
            if command == "/quit": break
            if command == "/show": output_fn(json.dumps(flow.to_record(), sort_keys=True)); continue
            if command in {"/back", "/lock"}:
                if len(parts) < 2:
                    output_fn("Command requires a stage name."); continue
                try:
                    target = Stage(parts[1])
                    flow = flow.back(target, parts[2] if len(parts) > 2 else "reopened for revision") if command == "/back" else flow.lock(target)
                    state_path.write_text(json.dumps(flow.to_record() | {"message_digests": digests}, sort_keys=True, indent=2) + "\n")
                    output_fn(f"Updated stage state; current stage: {flow.current.value if flow.current else 'COMPLETE'}")
                except (ValueError, StageFlowError) as error: output_fn(f"Stage command rejected: {error}")
                continue
            if command == "/reject": output_fn("Rejected; stage remains pending."); continue
            if command == "/approve":
                if flow.current is None: output_fn("Flow already complete."); continue
                reason = parts[1] if len(parts) > 1 else "approved"
                approver = parts[2] if len(parts) > 2 else "operator"
                try: flow = flow.approve(build_approval(flow.current, approver, reason))
                except StageFlowError as error: output_fn(f"Approval rejected: {error}"); continue
                state_path.write_text(json.dumps(flow.to_record() | {"message_digests": digests}, sort_keys=True, indent=2) + "\n")
                output_fn(f"Approved. Next stage: {flow.current.value if flow.current else 'COMPLETE'}")
                continue
            output_fn("Unknown command."); continue
        if text.strip(): digests.append(_digest(text)); output_fn("Proposal captured for this stage; review it, then use /approve.")
    return 0
