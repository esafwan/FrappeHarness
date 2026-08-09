# Frappe Harness Working Rules

## Authority and active plan

Read [`docs/PRIMARY_ROUTED_ASSISTED_FLOW_PLAN.md`](docs/PRIMARY_ROUTED_ASSISTED_FLOW_PLAN.md),
[`docs/DEPENDENCY_TRACKER.md`](docs/DEPENDENCY_TRACKER.md), and
[`docs/CURRENT_STATUS.md`](docs/CURRENT_STATUS.md) before changing code. Update
the tracker and current status after every accepted dependency slice.

The deterministic Python control plane and native typed Frappe provider are
authoritative. Frappe Commander may be explored on a disposable Bench but is
optional, non-gating, and must never become the only path to completion.

## Model boundary

- A model is an untrusted typed-data provider only. It has no shell, filesystem,
  Git, database, HTTP mutation, credential, approval, release, target-selection,
  or general tool authority.
- Every model role uses a fresh minimal, redacted context. The monitor returns
  only `CONTINUE`, `PAUSE`, `RETRY`, `ESCALATE`, or `STOP` plus allowed codes.
- Python owns route selection, schema validation, budgets, hashes, staleness,
  approval/lock checks, destructive-diff stops, and all execution. Python hard
  stops override any model recommendation.
- Do not persist or print prompts, raw model output, API keys, session cookies,
  or `.env` values. Retain normalized evidence only.

## Execution safety

- The native provider may execute only compiler-owned, hash-bound artifacts on
  the dedicated disposable Bench after exact intent approval, target lock,
  backup/checkpoint, and typed preflight. No arbitrary shell or REST endpoint.
- There is one live-Bench owner and one writer per file group. Other overlapping
  work is read-only audit only.
- Keep release autonomy distinct from assisted-flow acceptance: threshold-failed
  models can remain assisted-only, never promoted by wording or inference.

## Worker protocol

When delegating a bounded task, state tracker ID(s), exact owned files,
dependencies, deliverable, verification command, and exclusions. The integrator
reviews its diff and reruns verification before accepting it. Use a GPT fallback
only after recording a real Kimi auth/quota/token failure.

## Required verification

Before a handoff or integration run focused tests and, when applicable:

```bash
PYTHONPATH=src pytest -q
python3.11 -m compileall -q src tools
(cd frontend && npm run verify)
git diff --check
git diff --cached --check
```

Do not discard or overwrite unrelated work in this shared worktree. Historic
material is retained under `docs/archive/`.

