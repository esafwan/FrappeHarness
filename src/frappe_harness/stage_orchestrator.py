"""Read-only routing and stage-local contracts for the staged MVP.

The orchestrator may inspect state and recommend the next user-facing step,
but it cannot approve, mutate a Bench, or invoke tools.  Stage handlers are
isolated by an explicit prompt and action allow-list; Python remains the only
component that changes :class:`StageFlow`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from .stage_flow import Stage, StageFlow


@dataclass(frozen=True)
class PromptStageHandler:
    """Declarative stage boundary consumed by an external model adapter."""

    stage: Stage
    system_prompt: str
    allowed_actions: frozenset[str]


@dataclass(frozen=True)
class OrchestratorDecision:
    """A routing-only decision; it contains no executable action."""

    stage: Stage | None
    instruction: str
    allowed_next: tuple[Stage, ...]
    locked_stages: tuple[Stage, ...]
    complete: bool


class StageHandler(Protocol):
    """Contract implemented by a stage-specific model adapter."""

    stage: Stage
    system_prompt: str
    allowed_actions: frozenset[str]


def route(flow: StageFlow) -> OrchestratorDecision:
    """Inspect flow state and return the next safe conversational route."""
    current = flow.current
    if current is None:
        return OrchestratorDecision(None, "All stages are complete; prepare the final receipt.", (), tuple(sorted(flow.locked_stages, key=lambda s: s.value)), True)
    next_stage = Stage(current.value)
    return OrchestratorDecision(
        stage=next_stage,
        instruction=f"Stay within {next_stage.value}; gather inputs and request human approval.",
        allowed_next=(next_stage,),
        locked_stages=tuple(sorted(flow.locked_stages, key=lambda s: s.value)),
        complete=False,
    )


STAGE_HANDLERS: Mapping[Stage, StageHandler] = {
    Stage.PLAN: PromptStageHandler(Stage.PLAN, "You are the planning stage. Clarify scope, users, and acceptance criteria. Do not edit files or call tools.", frozenset({"ask", "summarize"})),
    Stage.DOCTYPES: PromptStageHandler(Stage.DOCTYPES, "You are the DocType stage. Propose typed DocType definitions only. Do not change permissions, UI, or release state.", frozenset({"ask", "propose_doctype", "summarize"})),
    Stage.PERMISSIONS: PromptStageHandler(Stage.PERMISSIONS, "You are the permissions stage. Review roles and access rules only. Do not mutate the site.", frozenset({"ask", "propose_permission", "summarize"})),
    Stage.UI_TESTS: PromptStageHandler(Stage.UI_TESTS, "You are the UI and test stage. Define checks and evidence only. Do not deploy or release.", frozenset({"ask", "propose_test", "summarize"})),
    Stage.RELEASE: PromptStageHandler(Stage.RELEASE, "You are the release stage. Review evidence and prepare a receipt. Never self-approve or bypass gates.", frozenset({"ask", "summarize"})),
}
