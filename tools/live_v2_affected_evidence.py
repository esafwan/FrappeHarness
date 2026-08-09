"""Create one fresh, read-only durable V2 affected-check evidence run.

This operator tool deliberately requires an explicit SQLite path and obtains
the Administrator password only from the current process environment. The
browser child receives it over stdin rather than an environment variable. It
does not record cookies, passwords, response bodies, or gate approvals.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess

import requests

from frappe_harness.contracts import spec_hash
from frappe_harness.frappe_affected_checks import BrowserAttestation, FrappeAffectedCheckConfig, FrappeAffectedCheckProvider
from frappe_harness.frappe_rest_provider import DocumentQuery, FrappeRestProvider, FrappeSessionCookie
from frappe_harness.modification_orchestration import RunBoundAffectedCheckExecutor
from frappe_harness.provider_plan import build_metadata_plan
from frappe_harness.run_store import LockTarget, RunIntent, RunState, RunStore
from frappe_harness.spec_io import load_project_spec


_BROWSER_PROBE = r'''
const { chromium } = require("playwright");
(async () => {
  const password = await new Promise((resolve) => {
    let value = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => { value += chunk; });
    process.stdin.on("end", () => resolve(value));
  });
  const browser = await chromium.launch({
    headless: true,
    args: [`--unsafely-treat-insecure-origin-as-secure=${process.env.HARNESS_BASE}`],
  });
  try {
    const page = await browser.newPage();
    await page.goto(process.env.HARNESS_BASE + "/login?redirect-to=%2Ftask_tracker.html", {waitUntil: "domcontentloaded"});
    await page.locator("#login_email").fill("Administrator");
    await page.locator("#login_password").fill(password);
    await page.getByRole("button", {name: "Continue", exact: true}).click();
    await page.waitForURL((url) => !url.pathname.endsWith("/login"), {timeout: 20000});
    const response = await page.goto(process.env.HARNESS_BASE + "/task_tracker.html", {waitUntil: "domcontentloaded"});
    if (!response || response.status() !== 200) throw new Error("generated page did not return HTTP 200");
    if (!(await page.content()).includes("frappe_harness_manifest_endpoint")) throw new Error("generated manifest bootstrap missing");
    await page.waitForTimeout(1500);
    if (!(await page.locator("body").innerText()).includes("Task")) throw new Error("generated Task UI did not render");
  } catch {
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
'''


def _browser_probe(target: LockTarget, base_url: str, password: str) -> BrowserAttestation:
    result = subprocess.run(
        ["node", "-e", _BROWSER_PROBE],
        cwd=Path(__file__).parents[1] / "frontend",
        env={**os.environ, "HARNESS_BASE": base_url},
        input=password.encode("utf-8"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=45,
        check=False,
    )
    return BrowserAttestation(
        target=target,
        url=base_url.rstrip("/") + "/task_tracker.html",
        passed=result.returncode == 0,
        status_code=200 if result.returncode == 0 else None,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--bench-path", required=True)
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--task-name")
    parser.add_argument("--spec", type=Path, default=Path("fixtures/task_tracker_v2_reference_url.json"))
    args = parser.parse_args()
    password = os.environ.get("FRAPPE_HARNESS_LIVE_ADMIN_PASSWORD")
    if not password:
        raise SystemExit("FRAPPE_HARNESS_LIVE_ADMIN_PASSWORD must be set in the current process")

    spec = load_project_spec(args.spec)
    plan = build_metadata_plan(spec, provider="frappe-native")
    spec_digest = spec_hash(spec)
    session = requests.Session()
    response = session.post(
        args.base_url.rstrip("/") + "/api/method/login",
        data={"usr": "Administrator", "pwd": password},
        timeout=20,
    )
    response.raise_for_status()
    cookie = "; ".join(f"{key}={value}" for key, value in session.cookies.get_dict().items())
    if not cookie:
        raise RuntimeError("login did not return a session cookie")
    rest = FrappeRestProvider(args.base_url, None, session_cookie=FrappeSessionCookie(cookie))
    if args.task_name:
        task_name = args.task_name
        record_result = rest.get_document("Task", task_name)
    else:
        record_result = rest.list_documents("Task", DocumentQuery(limit=1))
        task_name = None
    if not record_result.ok or not isinstance(record_result.payload, dict):
        raise RuntimeError("typed Task lookup failed")
    data = record_result.payload.get("data")
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
        raise RuntimeError("typed Task lookup returned no existing record")
    task_name = task_name or data["name"]
    expected = {key: data.get(key) for key in ("name", "title", "status", "project", "priority", "due_date") if key in data}

    store = RunStore(args.store)
    intent = RunIntent(LockTarget(args.bench_path, args.site, "task_tracker"), "frappe-native", spec_digest, plan.plan_hash)
    run = store.create_run(intent)
    store.transition(run.id, RunState.PENDING_APPROVAL)
    store.record_approval(run.id, approver="Administrator", approved=True, intent=intent, rationale="disposable read-only V2 checks")
    store.transition(run.id, RunState.APPROVED)
    store.transition(run.id, RunState.RUNNING)
    provider = FrappeAffectedCheckProvider(
        rest,
        FrappeAffectedCheckConfig(
            bench=args.bench_path, site=args.site, app="task_tracker", spec_hash=spec_digest,
            plan_hash=plan.plan_hash, task_name=task_name, expected_record=expected,
            browser_probe=lambda target: _browser_probe(target, args.base_url.rstrip("/"), password),
        ),
        store=store,
    )
    evidence = RunBoundAffectedCheckExecutor(store, provider).execute(
        run.id, expected_spec_hash=spec_digest, expected_plan_hash=plan.plan_hash,
    )
    print(json.dumps({
        "run_id": run.id, "store": str(args.store), "task_name": task_name,
        "spec_hash": spec_digest, "plan_hash": plan.plan_hash,
        "evidence_refs": [evidence.migration_ref, evidence.preservation_ref, evidence.browser_ref, evidence.destructive_no_mutation_ref],
        "gate_approval_recorded": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
