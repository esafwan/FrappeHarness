from frappe_harness.stage_flow import Stage, StageFlow, build_approval
from frappe_harness.stage_orchestrator import route


def _complete(flow, *stages):
    for stage in stages:
        assert flow.current is stage
        flow = flow.approve(build_approval(stage, "tester", "approved"))
    return flow


def test_router_is_read_only_and_routes_current_stage():
    flow = _complete(StageFlow(), Stage.PLAN)
    decision = route(flow)
    assert decision.stage is Stage.DOCTYPES
    assert decision.allowed_next == (Stage.DOCTYPES,)
    assert flow.current is Stage.DOCTYPES


def test_router_reports_locked_stages_without_actions():
    flow = _complete(StageFlow(), Stage.PLAN).lock(Stage.PLAN)
    decision = route(flow)
    assert decision.locked_stages == (Stage.PLAN,)
    assert decision.complete is False
    assert not hasattr(decision, "execute")

