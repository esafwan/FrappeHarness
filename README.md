# Frappe Small-Model Application Harness

Frappe Harness is a controlled development loop for building Frappe apps with
small language models. Models propose typed data only. Python validates and
routes that data; a human approves each stage; only the approved provider may
mutate a disposable Bench.

## What is implemented

- Strict typed route, monitor, watchdog, repair, replay, and evidence contracts.
- Deterministic compiler and native Frappe provider.
- Optional Commander provider using fixed, allowlisted REST endpoints.
- Human-approved staged flow: `PLAN → DOCTYPES → PERMISSIONS → UI_TESTS → RELEASE`.
- Durable approvals, target locks, backups/checkpoints, recovery, and
  independent metadata/permission verification.
- Gemma/Gemini integrations remain assisted typed-data providers; they do not
  receive shell, REST, database, credential, approval, or target authority.

The authoritative plan and evidence are in:

- [`docs/PRIMARY_ROUTED_ASSISTED_FLOW_PLAN.md`](docs/PRIMARY_ROUTED_ASSISTED_FLOW_PLAN.md)
- [`docs/DEPENDENCY_TRACKER.md`](docs/DEPENDENCY_TRACKER.md)
- [`docs/CURRENT_STATUS.md`](docs/CURRENT_STATUS.md)
- [`docs/COMMANDER_OPTIONAL_PROVIDER_GATE.md`](docs/COMMANDER_OPTIONAL_PROVIDER_GATE.md)
- [`skills/frappe-harness-staged/SKILL.md`](skills/frappe-harness-staged/SKILL.md)

## Install and verify

```bash
python3 -m pip install -e .
PYTHONPATH=src pytest -q
python3.11 -m compileall -q src tools
(cd frontend && npm install && npm run verify)
git diff --check
git diff --cached --check
```

For a source checkout, use `PYTHONPATH=src` instead of installing the package.

## Validate, plan, and compile an app

```bash
export PYTHONPATH=src
frappe-harness validate fixtures/task_tracker.json
frappe-harness plan fixtures/task_tracker.json
frappe-harness compile fixtures/task_tracker.json --output /tmp/task_tracker_compiled
```

The plan is semantic JSON. It is not a shell script and cannot select a target
or execute a mutation.

## Human-approved staged workflow

The CLI persists only normalized stage IDs, status, reason, and digest. Stages
cannot be skipped or approved out of order.

```bash
export PYTHONPATH=src
STATE=/tmp/student-management-flow.json

python3 -m frappe_harness.cli stage "$STATE"
python3 -m frappe_harness.cli stage "$STATE" --approve PLAN \
  --approver safwan --reason "Approved the student-management scope"
python3 -m frappe_harness.cli stage "$STATE" --approve DOCTYPES \
  --approver safwan --reason "Approved Student, Course, and Enrollment DocTypes"
python3 -m frappe_harness.cli stage "$STATE" --approve PERMISSIONS \
  --approver safwan --reason "Approved the role matrix"
python3 -m frappe_harness.cli stage "$STATE" --approve UI_TESTS \
  --approver safwan --reason "Approved the UI and test plan"
python3 -m frappe_harness.cli stage "$STATE" --approve RELEASE \
  --approver safwan --reason "Approved disposable release"
```

Inspect or replay the state by running the command without `--approve`:

```bash
python3 -m frappe_harness.cli stage "$STATE"
```

For a chat-like resumable terminal session, use the interactive MVP:

```bash
python3 -m frappe_harness.cli stage-chat "$STATE"
```

Type the current-stage proposal or discussion text, then use `/show`,
`/reject`, `/approve [reason] [approver]`, `/back STAGE [reason]`,
`/lock STAGE`, or `/quit`. `/back` reopens an earlier stage and invalidates
later approvals; `/lock` permanently prevents rollback across that completed
stage. Free-form text is kept
only as a digest; normalized stage state is what can resume between sessions.

Routing is deliberately split from execution. The read-only orchestrator only
reports the current stage and safe conversational route. Each stage has its
own prompt and allow-list (`stage_orchestrator.py`); it cannot approve, call
tools, or mutate a Bench. Python validation plus explicit human approval is
required before any stage transition.

## Disposable Bench lifecycle

Only use the dedicated disposable target for mutation:

```text
container: frappe_docker_devcontainer-frappe-1
bench:     /workspace/development/frappe-harness-live-20260804
site:      frappe-harness-live-20260804.local
app:       task_tracker
```

### Start the site

```bash
docker start frappe_docker_devcontainer-frappe-1
docker exec --workdir /workspace/development/frappe-harness-live-20260804 \
  frappe_docker_devcontainer-frappe-1 \
  bench --site frappe-harness-live-20260804.local serve --port 8000 --noreload
```

For a background process, run the `bench ... serve` command with `nohup` inside
the disposable container. Verify readiness without mutating data:

```bash
curl -H 'Host: frappe-harness-live-20260804.local' \
  -H 'Accept:' http://127.0.0.1:8000/api/method/ping
```

### Stop the site

Stop only the disposable web process or container. Do not stop protected
stacks by broad name or wildcard:

```bash
docker stop frappe_docker_devcontainer-frappe-1
```

### Create an app

Run this only inside a disposable Bench and choose an app slug deliberately:

```bash
docker exec --workdir /workspace/development/frappe-harness-live-20260804 \
  frappe_docker_devcontainer-frappe-1 \
  bench new-app student_management
```

### Install an app on a site

```bash
docker exec --workdir /workspace/development/frappe-harness-live-20260804 \
  frappe_docker_devcontainer-frappe-1 \
  bench --site frappe-harness-live-20260804.local install-app student_management
```

Then verify installation read-only:

```bash
docker exec --workdir /workspace/development/frappe-harness-live-20260804 \
  frappe_docker_devcontainer-frappe-1 \
  bench --site frappe-harness-live-20260804.local list-apps
```

### Create a new site (disposable only)

```bash
docker exec --workdir /workspace/development/frappe-harness-live-20260804 \
  frappe_docker_devcontainer-frappe-1 \
  bench new-site student-management.local
```

Do not run `new-site`, `drop-site`, `install-app`, or migration commands against
any protected or production-designated Bench.

## Run the local no-Bench demo

```bash
PYTHONPATH=src python3 -m frappe_harness.cli demo-run \
  --store /tmp/frappe-harness-demo.sqlite3
```

This exercises the lifecycle with injected data-only providers. It is not live
Bench evidence.

## Run the disposable native or Commander chain

The live command requires an approved schema-v2 operator bundle and a
digest-bound assisted route trace. The Administrator password is process-local;
never print it or commit it:

```bash
export FRAPPE_HARNESS_LIVE_ADMIN_PASSWORD=admin
export PATH="$PWD/tools:$PATH"
export PYTHONPATH=src

python3 tools/live_v1_v2_modification_evidence.py \
  --container frappe_docker_devcontainer-frappe-1 \
  --bench-path /workspace/development/frappe-harness-live-20260804 \
  --site frappe-harness-live-20260804.local \
  --app task_tracker \
  --base-url http://127.0.0.1:8000 \
  --store <STORE> \
  --bundle <OPERATOR_BUNDLE> \
  --route-trace <ROUTE_TRACE_JSON> \
  --provider frappe-native --execute
```

Use `--provider commander-spike` only for the signed disposable profile. Native
Frappe remains the default authoritative provider.

## Safety model

Human approval is required at every stage. Python owns route selection,
validation, target identity, locks, approvals, compilation, provider choice,
watchdog stops, recovery, and release decisions. A model may suggest a plan or
typed operation, but it cannot create a command, arbitrary endpoint, SQL, Git
mutation, credential, or approval.

Autonomous Gemma/Gemini release remains blocked by the recorded unsafe-admission
and accuracy thresholds. Constrained assisted profiles are allowed when their
use-case-specific validators and human gates pass.

## Troubleshooting

- `required_tool_unobserved`: acquire fresh typed Bench/frappectl facts; do not
  mark the tool available by assumption.
- `assisted watchdog did not authorize continuation`: inspect the route trace;
  approval, lock, checkpoint, digest, or budget prerequisites are missing.
- `Commander action kind ... not allow-listed`: keep the operation blocked until
  a fixed endpoint mapping and independent verification exist.
- `permission denied`: verify the approved role matrix and use an authorized
  disposable Administrator session; never bypass the permission gate.
