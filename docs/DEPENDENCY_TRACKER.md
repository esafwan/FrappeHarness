# Active Dependency Tracker

**Authority:** [`PRIMARY_ROUTED_ASSISTED_FLOW_PLAN.md`](PRIMARY_ROUTED_ASSISTED_FLOW_PLAN.md)  
**Updated:** 2026-08-09 (Asia/Dubai)  
**Status labels:** `Complete`, `In progress`, `Not started`, `Blocked`.

Historical V1 task and gate records are preserved in
[`archive/2026-08-09-pre-routed-flow/DEPENDENCY_TRACKER.md`](archive/2026-08-09-pre-routed-flow/DEPENDENCY_TRACKER.md).

| ID | Status | Depends on | Owned area | Exact completion evidence | Next action |
|---|---|---|---|---|---|
| RTE-00 | Complete | — | Existing native core and archived evidence | Immutable successor run `49582060-c35c-4b98-89b1-dde1cb0d423a` and its archived native lifecycle evidence establish the baseline; regression commands are required before every new integration | Preserve as regression baseline |
| RTE-01 | Complete | RTE-00 | `src/frappe_harness/route_contracts.py`, `tests/test_route_contracts.py` | Strict schema-v1 immutable `RouteRequest`/`RouteDecision` parser; unknown fields/routes, stale request IDs, disallowed routes, non-coercion, secret-shaped names/values, raw-output facts, bounded confidence, and immutable collection tests pass. Focused suite: 15 passed; full Python suite: 100% passed with 2 skips; compileall, frontend verify, and diff checks pass. | Begin independent RTE-02 adapter and RTE-03 route-policy tasks |
| RTE-02 | Complete | RTE-01 | `src/frappe_harness/route_adapter.py`, `tests/test_route_adapter.py` | Pure `RouteModelCaller`/`RouteClassifierAdapter` boundary binds request IDs and delegates output validation to the strict parser; malformed, stale, disallowed, untyped, and capability-import cases pass (6 focused tests). Kimi produced the adapter; the integrator supplied the missing focused test file after the worker stalled, with no auth/quota fallback. | Start RTE-04 after RTE-03 policy integration is stable |
| RTE-03 | Complete | RTE-01 | `src/frappe_harness/route_policy.py`, `tests/test_route_policy.py` | Explicit V1 `(state, operation)` transition table; every unknown pair, stale request, request-disallowed route, or policy-disallowed route fails closed to `ESCALATE`; immutable outcome and all-route table tests pass (50 focused tests). Kimi result inspected and accepted. | Start RTE-04 immutable monitor snapshot |
| RTE-04 | Complete | RTE-01, RTE-03 | `src/frappe_harness/monitor_contract.py`, `tests/test_monitor_contract.py` | Immutable schema-v1 `MonitorSnapshot` with deterministic SHA-256 binding, normalized redacted facts, control-state fields, unknown/raw-output/secret/type/digest rejection, and 8 focused tests passing. Kimi worker stalled without an auth/quota error; integrator completed and reviewed this bounded slice. | Start RTE-05 typed monitor adapter |
| RTE-05 | Complete | RTE-04 | `src/frappe_harness/monitor_adapter.py`, `tests/test_monitor_adapter.py` | Typed independent monitor adapter accepts only a bound snapshot and allow-listed `MonitorAction`; stale digest, unknown/raw fields, malformed action/reason shape, and untyped snapshot tests pass (6 focused tests). Kimi worker stalled without an auth/quota error; integrator completed and reviewed this bounded slice. | Integrate RTE-05 output into RTE-07 after RTE-06 |
| RTE-06 | Complete | RTE-03, RTE-04 | `src/frappe_harness/watchdog.py`, `tests/test_watchdog.py` | Python hard-stop watchdog overrides monitor recommendations for invalid digest, exhausted budget, missing approval/lock/checkpoint before mutation, and preserves valid typed actions otherwise; 11 focused tests pass. Kimi worker stalled without an auth/quota error; integrator completed and reviewed this bounded slice. | Integrate watchdog precedence into RTE-07 |
| RTE-07 | Complete | RTE-02, RTE-03, RTE-05, RTE-06 | `src/frappe_harness/assisted_flow.py`, `tests/test_assisted_flow.py` | Deterministic composition invokes classifier → policy → bounded worker result → monitor snapshot/adapter → watchdog; policy/worker failures stop before later stages, mutation prerequisites hard-stop, unknown worker fields fail closed. Five focused scenario tests pass. | Start RTE-08 bounded repair/clarification loop |
| RTE-08 | Complete | RTE-07 | `src/frappe_harness/repair_flow.py`, `tests/test_repair_flow.py` | One validator-issue-driven repair attempt; typed clarification or escalation on failure; disallowed route and unknown fields stop before caller. Four focused tests pass. | Start RTE-09 normalized trace/replay binding |
| RTE-09 | Complete | RTE-07 | `src/frappe_harness/route_trace.py`, `tests/test_route_trace.py` | Normalized trace record binds request/decision IDs, route, policy, worker, monitor, watchdog and digest; strict record round-trip reproduces the same next action without prompts/raw output. Focused replay test passes. | Start RTE-10 safe operator status surface |
| RTE-10 | Complete | RTE-07, RTE-09 | `src/frappe_harness/cli.py`, `tests/test_cli.py` | Read-only `route-status` CLI parses a strict persisted trace and emits only IDs, route, approval/hard-stop state, next action, and digest; normalized trace status test passes. It cannot execute or resume work. | Start RTE-11 model failure/workaround regression map |
| RTE-11 | Complete | RTE-03, RTE-08 | `src/frappe_harness/model_workarounds.py`, `tests/test_model_workarounds.py` | All 28 bundled evaluation case IDs have deterministic guard codes, route choices, and operator actions; unknown cases escalate. Corpus inventory and workaround tests pass. Autonomous model thresholds remain unchanged. | Start RTE-12 assisted disposable demonstration |
| RTE-12 | Complete | RTE-08, RTE-09, RTE-10, RTE-11 | `src/frappe_harness/assisted_admission.py`, `tools/live_v1_v2_modification_evidence.py`, disposable Bench | Digest-bound assisted trace admitted native execution. Disposable Frappe 16.29 Bench was repaired by restarting only its stale web process; `/api/method/ping` and Administrator login passed. Fresh typed preflight facts were acquired, the disposable app baseline was checkpointed at Git commit `539fdcfaf945268b68c6243040f5d33079402803`, and one durable V1→V2 run completed with deployment reference `docker-generated-artifacts:0c69388df55242122ae6d052e4f505c122555582c9354aa03ec4a374e0cf0308`, successor run `2884840a-3f83-44de-8329-349d245d15c9`, and evidence receipt `run:2884840a-3f83-44de-8329-349d245d15c9:evidence:a88c637f-08e7-46db-a802-2f910d7f7c88`. | RTE-GATE is audited; preserve the disposable-only scope |
| RTE-GATE | Complete | RTE-12 | Requirement audit + evidence pack | Full Python suite passed (`pytest`: 758 passed, 2 skipped), Python 3.11 compileall passed, frontend typecheck/Vitest (20)/Vite build passed, both diff checks passed; RTE-01–RTE-12 evidence and the disposable native receipt are recorded above. | Autonomous model release remains separately blocked by the documented Gemma corpus threshold; the harness itself is usable under routed deterministic control |

## Dependency view

```text
RTE-00 → RTE-01 → {RTE-02, RTE-03}
RTE-03 + RTE-01 → RTE-04 → RTE-05
RTE-03 + RTE-04 → RTE-06
RTE-02 + RTE-03 + RTE-05 + RTE-06 → RTE-07
RTE-07 → {RTE-08, RTE-09}
RTE-07 + RTE-09 → RTE-10
RTE-03 + RTE-08 → RTE-11
RTE-08 + RTE-09 + RTE-10 + RTE-11 → RTE-12 → RTE-GATE

## Optional Commander enablement

| CMD-01 | Complete | RTE-GATE | Live endpoint audit | Disposable Commander commit `473b091c…` exposes five fixed REST endpoints; read-only documentation probe passed. | Keep endpoint surface fixed |
| CMD-02 | Complete | CMD-01 | Typed REST client and semantic mapping | `CommanderRestClient` maps `create_doctype`/`add_field`, validates payloads, redacts failures, and supports existing-DocType metadata probes. Focused tests and full regression pass. | Preserve fail-closed allowlist |
| CMD-03 | Complete | CMD-02 | Disposable Commander execution | Parent `371d6918-ab84-4b18-b25a-38d38f4cf39a`, successor `481eaa0c-6697-4195-98d7-39f76ecd682c`, evidence `run:481eaa0c-6697-4195-98d7-39f76ecd682c:evidence:eecbcab7-c42d-4b98-97c2-aa692c5a0c47`; independent metadata readback confirmed `reference_url` and Task Manager/User permissions. | Commander enabled only for disposable profile; native remains default |
| HIL-01 | Complete | RTE-07, RTE-09 | `src/frappe_harness/stage_flow.py`, `src/frappe_harness/cli.py` | Pure ordered human-approval flow for PLAN → DOCTYPES → PERMISSIONS → UI_TESTS → RELEASE; approvals are digest-bound, out-of-order/skipped stages fail closed, and normalized replayable state is persisted by the CLI. Full Python suite passes. | Use `frappe-harness stage` for staged proposal review |
```

## Non-negotiable gates

- Only deterministic Python selects the next executable state.
- A model can never receive a general mutation tool or secrets.
- The native Frappe provider is authoritative; Commander is optional and not a
  dependency.
- Live mutation occurs only on the dedicated disposable Bench under an existing
  target lock, approval, backup/checkpoint, compiler ownership check, and
  typed verification plan.
- A task is `Complete` only after its declared evidence is reviewed by the
  integrator and its focused/full verification is recorded here and in Current
  Status.
