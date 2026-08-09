import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const packageRoot = new URL("..", import.meta.url).pathname;
const repoRoot = new URL("../../..", import.meta.url).pathname;
const fixture = join(repoRoot, "fixtures", "task_tracker.json");
const output = mkdtempSync(join(tmpdir(), "frappe-harness-pi-equivalence-"));
try {
  writeFileSync(join(output, "package.json"), '{"type":"module"}\n');
  execFileSync(join(packageRoot, "node_modules", ".bin", "tsc"), [
    "src/index.ts", "--outDir", output, "--target", "ES2022", "--module", "ESNext",
    "--moduleResolution", "Bundler", "--strict", "--skipLibCheck",
  ], { cwd: packageRoot, stdio: "inherit" });
  const api = await import(pathToFileURL(join(output, "index.js")).href);
  const coreCommand = process.env.FRAPPE_HARNESS_BIN || null;
  const run = () => JSON.parse(execFileSync(
    coreCommand || "python3.11",
    coreCommand ? ["validate", fixture] : ["-m", "frappe_harness.cli", "validate", fixture],
    { cwd: repoRoot, env: { ...process.env, PYTHONPATH: join(repoRoot, "src") }, encoding: "utf8" },
  ));
  const first = run();
  const second = run();
  if (JSON.stringify(first) !== JSON.stringify(second) || first.valid !== true || typeof first.spec_hash !== "string") {
    throw new Error("core CLI output is not deterministic or valid");
  }
  const request = api.validateRequest({ request_id: "equiv-1", operation: "get_validation_report", payload: first });
  const envelope = api.bindHeadlessRequest(request, { session_id: "equiv-session", authoritative: false });
  if (envelope.protocol_version !== 1 || envelope.request.payload.spec_hash !== first.spec_hash) {
    throw new Error("headless envelope changed core validation output");
  }
  for (const payload of [{ path: "../secret" }, { approval: "operator" }]) {
    try {
      api.validateRequest({ request_id: "stale-1", operation: "harness_status", payload });
      throw new Error("authority-shaped stale payload was accepted");
    } catch (error) {
      if (!(error instanceof TypeError)) throw error;
    }
  }
  console.log(JSON.stringify({ deterministic: true, valid: first.valid, spec_hash: first.spec_hash, authoritative: envelope.session.authoritative, stale_payloads_rejected: 2 }));
} finally {
  rmSync(output, { recursive: true, force: true });
}
