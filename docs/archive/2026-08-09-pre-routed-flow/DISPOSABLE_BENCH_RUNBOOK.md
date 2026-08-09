# Disposable Bench operator runbook

This runbook is for the isolated development target only:
`frappe-harness-live-20260804`, site
`frappe-harness-live-20260804.local`, web port `8111`. It is an operator
procedure, not live evidence or approval. Do not use these commands against a
shared or production Bench.

## Restart and verify the isolated target

First confirm the exact disposable Bench directory before taking any action;
stop if the directory or site identity does not match:

```sh
cd /Users/safwan/Code/Docker/frappe_docker/development/frappe-harness-live-20260804
pwd
cat BENCH_IDENTITY.md | rg 'frappe-harness-live-20260804|8111'
```

Start the Bench in the foreground from that exact directory only inside the
same disposable runtime that provides its configured `mariadb`, `redis-cache`,
and `redis-queue` hostnames and its pinned Node runtime. A host shell that
cannot resolve those dependencies must stop; do not rewrite service topology,
change the Procfile, run a broad `docker compose down`, prune, volume cleanup,
or restart an unrelated container:

```sh
getent hosts mariadb redis-cache redis-queue
test -x /home/frappe/.nvm/versions/node/v24.13.0/bin/node
bench start
```

Wait for the web endpoint and verify the expected ping response and port:

```sh
curl --fail --silent --show-error \
  http://127.0.0.1:8111/api/method/ping
```

If either check fails, stop and investigate the disposable container only.
Do not claim a successful live run from container health alone.

## Build and deploy the generated frontend bundle

Frontend packaging is intentionally separate from `compile_project`; perform
this step only after the disposable identity checks above. Build with the
repository's pinned Node version, package the result, and write each returned
artifact to the exact inner-app path (never to the app root):

```sh
cd /Users/safwan/Code/Experiments/FrappeHarness/frontend
npm ci
npm run build
cd /Users/safwan/Code/Experiments/FrappeHarness
PYTHONPATH=src python3.11 - <<'PY'
from pathlib import Path
from frappe_harness.frontend_deployment import package_frontend_dist

bench = Path('/workspace/development/frappe-harness-live-20260804')
artifacts = package_frontend_dist('task_tracker', Path('frontend/dist'))
for relative, payload in artifacts.items():
    target = bench / 'apps' / 'task_tracker' / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
PY
docker exec frappe_docker_devcontainer-frappe-1 sh -lc \
  'cd /workspace/development/frappe-harness-live-20260804 && bench build && bench --site frappe-harness-live-20260804.local migrate'
```

Verify the page, immutable asset, and manifest endpoint after the rebuild. A
Guest request to the manifest must be denied; an authenticated request may be
used by the evidence tool below. If any artifact is missing or served from an
app-root `public`/`www` directory, stop and redeploy from the packaged mapping.

## Create fresh V2 affected-check evidence

Historical fixture references and prior run IDs cannot be reopened as durable
evidence. After confirming that the generated `task_tracker.html` page and
the V2 `Reference URL` metadata are actually deployed, use the checked-in
operator tool with an explicit store path:

```sh
FRAPPE_HARNESS_LIVE_ADMIN_PASSWORD='(process-local)' \
PYTHONPATH=src python3.11 tools/live_v2_affected_evidence.py \
  --base-url http://127.0.0.1:8111 \
  --site frappe-harness-live-20260804.local \
  --bench-path /workspace/development/frappe-harness-live-20260804 \
  --store /path/outside/repository/v2-evidence.sqlite3
```

The tool creates a fresh approved/running run, performs typed REST metadata and
record-preservation checks plus a read-only Playwright browser probe, and
prints only the run ID, hashes, and same-run evidence references. It never
records a gate approval, password, cookie, or response body. If the browser
probe or any typed check fails, do not use the partial store as gate evidence.

## Run the read-only Task User browser proof

Use an existing Task User and an existing Task record. Obtain the three
process-local inputs interactively; never put the password in shell history,
source, fixtures, logs, command receipts, or evidence:

```sh
read -r 'FRAPPE_HARNESS_LIVE_TASK_USER?Task User login: '
read -r -s 'FRAPPE_HARNESS_LIVE_TASK_PASSWORD?Task User password: '; printf '\n'
read -r 'FRAPPE_HARNESS_LIVE_TASK_RECORD_PATH?Existing Task path: '
export FRAPPE_HARNESS_LIVE_TASK_USER FRAPPE_HARNESS_LIVE_TASK_PASSWORD FRAPPE_HARNESS_LIVE_TASK_RECORD_PATH
export FRAPPE_HARNESS_LIVE_BASE_URL=http://127.0.0.1:8111
export FRAPPE_HARNESS_LIVE_ALLOW_MUTATION=0
```

From the frontend directory, run the opt-in live test:

```sh
cd frontend
npm run test:e2e:live
```

The test must remain read-only (`FRAPPE_HARNESS_LIVE_ALLOW_MUTATION=0`), and
the record path must identify an existing Task. A passing test proves only the
Task User delete-button visibility contract; it does not prove Bench
deployment, CSRF, provider reconciliation, or release approval.

## Full disposable identity and V1→V2 modification evidence

The checked-in full-chain entry point is currently a fail-closed readiness
probe, not migration evidence. It accepts only the exact disposable
container/Bench/site/app tuple, requires a fresh SQLite path outside this
repository, and runs only typed `version`, `list-apps`, and `show-config`
operations through `DockerBenchRunner`:

```sh
FRAPPE_HARNESS_LIVE_ADMIN_PASSWORD='(process-local)' \
PYTHONPATH=src python3.11 tools/live_v1_v2_modification_evidence.py \
  --container frappe_docker_devcontainer-frappe-1 \
  --bench-path /workspace/development/frappe-harness-live-20260804 \
  --site frappe-harness-live-20260804.local \
  --base-url http://127.0.0.1:8111 \
  --store /absolute/path/outside/repository/v1-v2-evidence.sqlite3
```

Until a closed generated-artifact deployer and a clean source-checkpoint
provider are implemented, the command exits `2`, reports both blockers, and
creates no ledger. A readiness response is not live evidence and must never be
used to approve `MOD-GATE`. Raw `docker cp`, shell callbacks, historical
checkpoint strings, or pre-existing evidence stores are not acceptable
substitutes.

For the full-plan integration proof, use a fresh external SQLite store and the
typed `FrappeDisposableTestIdentityProvider` with a process-local privileged
session cookie. Provision only the declared `Task Manager` and `Task User`
roles through the closed REST provider, run the browser/REST checks, and always
execute cleanup in a `finally` path. Retain only the public profile IDs and
same-run `test_identity_provisioned`/`test_identity_cleaned` references; never
retain passwords, cookies, response bodies, or user payloads.

The modification proof must use one ledger: create and approve the V1 parent,
record backup/checkpoint and migration evidence, assess the exact V2 fixture
with explicit reconfirmation, create the lock-transferred successor, obtain a
fresh V2 approval, perform the V2 migration, run all four affected checks, and
record `safe_modification` plus `destructive_no_mutation` evidence. The
successor-aware gate binding validates `revision_of`, the parent assessment
reference, and the V2 execution intent. Do not record a `MOD-GATE` approval as
part of acquisition; that decision remains an explicit operator action.

Remove the process-local values immediately after the run:

```sh
unset FRAPPE_HARNESS_LIVE_TASK_USER FRAPPE_HARNESS_LIVE_TASK_PASSWORD \
  FRAPPE_HARNESS_LIVE_TASK_RECORD_PATH FRAPPE_HARNESS_LIVE_BASE_URL \
  FRAPPE_HARNESS_LIVE_ALLOW_MUTATION
```

## Clean stop

When the disposable session is finished, stop the foreground `bench start`
process with Ctrl-C from that terminal. Do not kill unrelated processes or
remove volumes, databases, backups, or other containers. Record any operator
observations separately; do not convert this procedure into evidence
unless the run actually completed and its outputs were reviewed.
