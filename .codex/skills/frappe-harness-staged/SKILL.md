---
name: frappe-harness-staged
description: Build and operate Frappe apps through the Frappe Harness staged human-approval workflow, using small models only for typed proposals and Python for all validation, approvals, execution, recovery, and release authority.
---

# Frappe Harness staged workflow

Use this skill when building or changing a Frappe app with the harness.

## Required workflow

1. Read `AGENTS.md`, `docs/PRIMARY_ROUTED_ASSISTED_FLOW_PLAN.md`,
   `docs/DEPENDENCY_TRACKER.md`, and `docs/CURRENT_STATUS.md`.
2. Keep the model boundary data-only. Reject raw prompts, shell, SQL, arbitrary
   URLs, credentials, target selection, and approval claims.
3. Drive stages in this exact order:
   `PLAN → DOCTYPES → PERMISSIONS → UI_TESTS → RELEASE`.
4. At each stage, produce a typed proposal, validate it in Python, show the
   normalized diff/evidence, and stop for explicit human approval.
5. Never skip a stage or infer approval from a model response.
6. Use only the dedicated disposable Bench for mutation. Native Frappe is the
   default provider; Commander is optional and only allowed on its attested
   disposable profile.
7. Before completion, run the focused tests, full Python suite, compileall,
   frontend verification when relevant, and both diff checks.

## CLI stage control

```bash
export PYTHONPATH=src
STATE=/tmp/app-flow.json
python3 -m frappe_harness.cli stage "$STATE"
python3 -m frappe_harness.cli stage "$STATE" --approve PLAN --approver NAME --reason "..."
```

Repeat approvals for `DOCTYPES`, `PERMISSIONS`, `UI_TESTS`, and `RELEASE` in
order. The state file contains normalized records and a digest only.

## Bench rules

Use the exact disposable target documented in the repository README. Start or
stop only that site/container. `bench new-app`, `install-app`, `migrate`, and
other mutations require an explicit human-approved stage and must never target
protected stacks.

## Model use

Gemma/Gemini may propose typed plans, fields, permissions, tests, or explanations.
Python must validate every result and independently verify every mutation.
Autonomous model release is not implied by a successful assisted run.

## Handoff evidence

Record the stage digest, approval identity, provider, target, command/test
evidence, and next stage in the dependency tracker. Do not persist secrets,
session cookies, raw model output, or `.env` values.
