---
name: frappe-harness-staged
description: Build and operate Frappe apps through the Frappe Harness staged human-approval workflow, using small models only for typed proposals and Python for validation, approvals, execution, recovery, and release authority.
---

# Frappe Harness staged workflow

Use this skill when building or changing a Frappe app with the harness.

1. Read `AGENTS.md`, the primary plan, dependency tracker, and current status.
2. Keep model output data-only: reject shell, SQL, arbitrary URLs, credentials,
   target selection, approval claims, prompts, and raw output.
3. Drive stages in order: `PLAN → DOCTYPES → PERMISSIONS → UI_TESTS → RELEASE`.
4. At every stage, validate typed proposals in Python, show normalized evidence,
   and stop for explicit human approval. Never infer approval from a model.
5. Mutate only the dedicated disposable Bench. Native Frappe is the default;
   Commander is optional and restricted to its attested disposable profile.
6. Run focused tests, `PYTHONPATH=src pytest -q`, compileall, frontend verify
   when relevant, and both diff checks before handoff.

## CLI stage control

```bash
export PYTHONPATH=src
STATE=/tmp/app-flow.json
python3 -m frappe_harness.cli stage "$STATE"
python3 -m frappe_harness.cli stage "$STATE" \
  --approve PLAN --approver NAME --reason "approved scope"
```

Approve `DOCTYPES`, `PERMISSIONS`, `UI_TESTS`, and `RELEASE` in order. The state
file retains normalized records and a digest only.

## Safety and handoff

Use only the exact disposable target documented in the repository README.
Gemma/Gemini may propose typed plans, fields, permissions, tests, or explanations;
Python validates and independently verifies every mutation. Record stage digest,
approval identity, provider, target, tests, and next stage. Never persist secrets,
cookies, raw model output, or `.env` values.
