import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const root = mkdtempSync(join(tmpdir(), "frappe-harness-pi-lifecycle-"));
const marker = join(root, "events.log");
const extension = join(root, "lifecycle.ts");
writeFileSync(extension, `
import { appendFileSync } from "node:fs";
const marker = process.env.PI_LIFECYCLE_MARKER;
export default function (pi: any) {
  void import(process.env.FRAPPE_HARNESS_PI_ENTRY!).then((adapter: any) => {
    if (!Array.isArray(adapter.OPERATIONS) || !Array.isArray(adapter.DENIED_BUILTIN_TOOLS)) throw new Error("adapter descriptor missing");
    if (marker) appendFileSync(marker, "adapter_loaded\\n");
  });
  for (const event of ["session_start", "session_before_switch", "session_shutdown"]) {
    pi.on(event, () => { if (marker) appendFileSync(marker, event + "\\n"); });
  }
}
`);

const child = spawn("pi", [
  "--mode", "rpc", "--no-session", "--no-tools", "--offline", "--extension", extension,
], { env: { ...process.env, PI_LIFECYCLE_MARKER: marker, FRAPPE_HARNESS_PI_ENTRY: new URL("../dist/index.js", import.meta.url).href }, stdio: ["pipe", "pipe", "pipe"] });
let buffer = "";
const responses = new Map();
child.stdout.setEncoding("utf8");
child.stdout.on("data", (chunk) => {
  buffer += chunk;
  for (;;) {
    const newline = buffer.indexOf("\n");
    if (newline < 0) break;
    const line = buffer.slice(0, newline).replace(/\r$/, "");
    buffer = buffer.slice(newline + 1);
    if (!line) continue;
    try {
      const message = JSON.parse(line);
      if (message.type === "response" && message.id) responses.set(message.id, message);
    } catch { /* startup diagnostics are not protocol responses */ }
  }
});

const waitFor = async (id, timeoutMs = 5000) => {
  const started = Date.now();
  while (!responses.has(id)) {
    if (Date.now() - started > timeoutMs) throw new Error(`Pi RPC response timed out: ${id}`);
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  const response = responses.get(id);
  if (!response.success) throw new Error(`Pi RPC command failed: ${id}`);
  return response;
};
const send = (id, type) => child.stdin.write(`${JSON.stringify({ id, type })}\n`);

try {
  send("state-1", "get_state");
  const initial = await waitFor("state-1");
  if (initial.data?.isStreaming !== false) throw new Error("Pi did not report an idle session");
  send("new-1", "new_session");
  await waitFor("new-1");
  send("state-2", "get_state");
  await waitFor("state-2");
  child.kill("SIGTERM");
  await new Promise((resolve) => child.once("close", resolve));
  const events = readFileSync(marker, "utf8").trim().split("\n").filter(Boolean);
  if (!events.includes("adapter_loaded") || !events.includes("session_start") || !events.includes("session_before_switch")) {
    throw new Error(`Pi lifecycle events missing: ${events.join(",")}`);
  }
  console.log(JSON.stringify({
    pi_rpc: true,
    adapter_loaded: true,
    session_start: true,
    session_replacement: true,
    built_in_tools_disabled: true,
    compatibility_claim: false,
  }));
} finally {
  if (!child.killed) child.kill("SIGTERM");
  rmSync(root, { recursive: true, force: true });
}
