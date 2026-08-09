# Current Status and Resume Guide

**Updated:** 2026-08-09 (Asia/Dubai)  
**Current phase:** Routed assisted-flow control plane — RTE-01–12 and RTE-GATE complete  
**Primary authority:** [`PRIMARY_ROUTED_ASSISTED_FLOW_PLAN.md`](PRIMARY_ROUTED_ASSISTED_FLOW_PLAN.md)

## One-line status

The native deterministic Frappe harness is demonstrably valuable and proven on
a disposable Frappe-16 V1→V2 change; the routed/monitored Python control plane
is implemented through RTE-12, including an authoritative disposable V1→V2
receipt; the requirement audit and regression evidence are complete.

## What is complete and usable

- The native compiler/provider path is real: it performed an approved,
  recoverable, verified disposable Frappe-16 V1→V2 `Reference URL` change while
  preserving records and proving browser/session and destructive-no-mutation
  behavior. The immutable successor run is
  `49582060-c35c-4b98-89b1-dde1cb0d423a`; its detailed receipt and verification
  references are preserved in the [archived status record](archive/2026-08-09-pre-routed-flow/CURRENT_STATUS.md).
- Strict loaders, compiler ownership/hashes, durable run lifecycle, approvals,
  target locks, typed Frappe REST/provider operations, recovery controls, and
  frontend checks already constrain the execution path.
- The actual evidence, commands, gates, Bench details, and model results remain
  in the dated archive. Frappe Commander is not required for this path.

## Why the new flow is needed

Gemma4 and Gemini Flash can produce useful bounded proposals, but the recorded
corpus has unsafe admissions and accuracy failures. That means they must not
drive an autonomous path. The new flow turns them into tightly controlled
specialists: a tiny route decision, a route-specific typed response, and an
independent monitor recommendation; Python validates, steers, pauses, or stops.

## Next exact action

**RTE-01 is complete:** `src/frappe_harness/route_contracts.py` and
`tests/test_route_contracts.py` provide strict schema-v1 immutable route
envelopes, correlation/staleness binding, allow-listed routes, non-coercion,
secret/raw-output rejection, and negative tests. Focused tests (15), the full
Python suite, compileall, frontend verification, and diff checks passed.
RTE-02 and RTE-03 are now also complete: the pure classifier adapter binds
model output to the strict route contract, and the deterministic V1 policy maps
state/operation pairs to explicit routes with fail-closed escalation. Their
focused suites pass (6 and 50 tests respectively). RTE-04 is also complete:
`MonitorSnapshot` is immutable, redacted, digest-bound, and fail-closed on
unknown, secret-shaped, raw-output, stale, and malformed data (8 focused tests).
RTE-05 and RTE-06 are now complete: the monitor adapter accepts only a
snapshot-bound `MonitorAction`, while the Python watchdog hard-stops invalid
digest, budget, approval, lock, or checkpoint states before mutation. Their
focused suites pass (6 and 11 tests respectively). RTE-07 is complete: it
composes router, worker, validator, monitor, and watchdog.
RTE-07 is complete: `AssistedFlowCoordinator` composes the typed classifier,
policy, worker boundary, monitor snapshot/adapter, and watchdog in one
deterministic no-tool flow. Policy escalation and worker rejection prevent
later stages, and mutation prerequisites are hard-stopped. Five composition
scenarios pass. RTE-08 and RTE-09 are also complete: repair is limited to one
typed attempt with finite clarification/escalation, and normalized route traces
can be persisted and replayed to the same safe next action without raw model
context. Their focused tests pass (4 and 1). RTE-10 is also complete: the
read-only `route-status` CLI displays only normalized trace IDs, route, safety
state, next action, and digest. RTE-11 is also complete: all 28 bundled model
cases map to deterministic guard codes, route choices, and operator actions;
unknown cases escalate. RTE-12 is complete: live execution required and
accepted a digest-bound assisted trace whose policy, worker, and watchdog all
authorized `CONTINUE`. The dedicated disposable web process was restarted,
typed preflight facts and an Administrator test session passed, and one
durable native V1→V2 chain completed with deployment, recovery, and evidence
receipts. No protected stack was touched. Autonomous model release remains
separately blocked by the documented model evaluation threshold; the assisted
harness itself is complete and usable.

## Remaining delivery

`RTE-01 → RTE-02/RTE-03 → RTE-04/RTE-05/RTE-06 → RTE-07 → RTE-08/RTE-09 → RTE-10/RTE-11 → RTE-12 → RTE-GATE`.

The assisted-flow goal is accepted because this chain proves a full native
disposable run. Autonomous small-model release is a separate, intentionally
unmet policy decision until a candidate passes the archived thresholds with zero
unsafe admissions.

## Safe resume commands

Run these before and after an implementation slice from the repository root:

```bash
PYTHONPATH=src pytest -q
python3.11 -m compileall -q src tools
(cd frontend && npm run verify)
git diff --check
git diff --cached --check
```

Use only the dedicated disposable Bench for RTE-12. Never print `.env` values
or retain raw model responses.
