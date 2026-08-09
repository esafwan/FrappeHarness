# Fixed frontend local browser contract

`npm run test:e2e` starts the local Vite development server and intercepts the
same-origin manifest and standard `/api/resource/*` requests in Playwright.
It proves manifest-bounded browser behaviour only. It does **not** sign into
Frappe, provision a user, call a live Bench, deploy assets, or prove CSRF and
session integration. Those require the separately approved Frappe embedding
and test-user lifecycle decisions.

The real-Bench proof is separate and opt-in. It is read-only and requires an
existing Task User session target; it does not create/delete records or write
evidence:

```sh
FRAPPE_HARNESS_LIVE_BASE_URL=https://disposable-bench.example \
FRAPPE_HARNESS_LIVE_TASK_USER=task-user@example.com \
FRAPPE_HARNESS_LIVE_TASK_PASSWORD='process-local' \
FRAPPE_HARNESS_LIVE_TASK_RECORD_PATH='/task_tracker/task/TASK-READONLY' \
FRAPPE_HARNESS_LIVE_ALLOW_MUTATION=0 \
npm run test:e2e:live
```

Do not put credentials in source, fixtures, logs, or evidence. A passing run
proves only the Task User Delete-button visibility contract unless an operator
separately records the live evidence. The base URL must be an http(s) origin
without embedded credentials, and the record path must be a same-origin
relative path.
