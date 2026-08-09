import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const root = new URL("..", import.meta.url).pathname;
const output = mkdtempSync(join(tmpdir(), "frappe-harness-pi-contract-"));
try {
  writeFileSync(join(output, "package.json"), '{"type":"module"}\n');
  execFileSync(join(root, "node_modules", ".bin", "tsc"), [
    "src/index.ts", "--outDir", output, "--target", "ES2022", "--module", "ESNext",
    "--moduleResolution", "Bundler", "--strict", "--skipLibCheck",
  ], { cwd: root, stdio: "inherit" });
  const api = await import(pathToFileURL(join(output, "index.js")).href);
  const valid = { request_id: "req-1", operation: "harness_status", payload: { label: "demo" } };
  if (api.validateRequest(valid).request_id !== "req-1") throw new Error("valid request was rejected");
  const session = { session_id: "session-1", authoritative: false };
  if (api.bindHeadlessRequest(valid, session).protocol_version !== 1) throw new Error("headless binding failed");
  const rejects = [
    [{ request_id: "req-2", operation: "unknown", payload: {} }, "operation"],
    [{ request_id: "req-3", operation: "harness_status", payload: { path: "/tmp/x" } }, "authority"],
    [{ request_id: "req-4", operation: "harness_status", payload: { text: "sudo rm -rf /" } }, "shell"],
  ];
  for (const [value, label] of rejects) {
    try {
      api.validateRequest(value);
      throw new Error(`${label} payload was accepted`);
    } catch (error) {
      if (!(error instanceof TypeError)) throw error;
    }
  }
  try {
    api.bindHeadlessRequest(valid, { session_id: "bad", authoritative: true });
    throw new Error("authoritative session was accepted");
  } catch (error) {
    if (!(error instanceof TypeError)) throw error;
  }
  console.log(JSON.stringify({ valid_request: true, invalid_requests_rejected: rejects.length, authoritative_session_rejected: true }));
} finally {
  rmSync(output, { recursive: true, force: true });
}
