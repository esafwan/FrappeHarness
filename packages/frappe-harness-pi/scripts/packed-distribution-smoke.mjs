import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const packageRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const scratch = mkdtempSync(join(tmpdir(), "frappe-harness-pi-pack-"));
const installRoot = join(scratch, "install");

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: packageRoot,
    encoding: "utf8",
    env: { ...process.env, PI_OFFLINE: "1" },
    ...options,
  });
  if (result.error || result.status !== 0) {
    throw new Error(`${command} failed: ${result.stderr || result.error?.message || "unknown error"}`);
  }
  return result.stdout;
}

async function loadWithPi(entry) {
  const child = spawn("pi", [
    "--mode", "rpc", "--no-session", "--no-tools", "--offline",
    "--no-extensions", "--no-skills", "--extension", entry,
  ], {
    env: { ...process.env, PI_OFFLINE: "1" },
    stdio: ["pipe", "pipe", "pipe"],
  });
  let stdout = "";
  let stderr = "";
  const closed = new Promise((resolve) => child.once("close", resolve));
  const launched = new Promise((resolve, reject) => {
    child.once("spawn", resolve);
    child.once("error", reject);
  });
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const deadline = Date.now() + 10000;
  try {
    await launched;
    child.stdin.write(`${JSON.stringify({ id: "packed-state", type: "get_state" })}\n`);
    for (;;) {
      for (const line of stdout.split(/\r?\n/).filter(Boolean)) {
        try {
          const message = JSON.parse(line);
          if (message.type === "response" && message.id === "packed-state") {
            if (!message.success || message.data?.isStreaming !== false) {
              throw new Error("Pi packed-extension state response was invalid");
            }
            if (/extension.*(?:fail|error)/i.test(stderr)) {
              throw new Error(`Pi reported a packed-extension load error: ${stderr}`);
            }
            return;
          }
        } catch (error) {
          if (error instanceof SyntaxError) continue;
          throw error;
        }
      }
      if (child.exitCode !== null) throw new Error(`Pi exited before packed extension loaded: ${stderr}`);
      if (Date.now() > deadline) throw new Error(`Pi packed-extension load timed out: ${stderr}`);
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
  } finally {
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGTERM");
    const closedPromptly = await Promise.race([
      closed.then(() => true),
      new Promise((resolve) => setTimeout(() => resolve(false), 2000)),
    ]);
    if (!closedPromptly && child.exitCode === null && child.signalCode === null) {
      child.kill("SIGKILL");
      await closed;
    }
  }
}

try {
  const pack = JSON.parse(run("npm", [
    "pack", "--json", "--ignore-scripts", "--pack-destination", scratch,
  ]));
  if (!Array.isArray(pack) || pack.length !== 1) throw new Error("npm pack returned an unexpected manifest");
  const packedFiles = pack[0].files.map(({ path }) => path).sort();
  const required = ["dist/index.d.ts", "dist/index.js", "package.json"];
  for (const path of required) {
    if (!packedFiles.includes(path)) throw new Error(`packed artifact is missing ${path}`);
  }
  if (packedFiles.some((path) => path.startsWith("src/") || path.startsWith("node_modules/"))) {
    throw new Error("packed artifact contains source or node_modules content");
  }

  const tarball = join(scratch, pack[0].filename);
  run("npm", [
    "install", "--ignore-scripts", "--no-audit", "--no-fund", "--no-package-lock",
    "--engine-strict=false", "--prefix", installRoot, tarball,
  ]);
  const installedRoot = join(installRoot, "node_modules", "@frappe-harness", "frappe-harness-pi");
  const manifest = JSON.parse(readFileSync(join(installedRoot, "package.json"), "utf8"));

  if (!manifest.pi?.extensions?.length) {
    throw new Error("packed package.json is missing the Pi manifest extensions entry");
  }
  for (const ext of manifest.pi.extensions) {
    const extPath = join(installedRoot, ext);
    try {
      readFileSync(extPath);
    } catch {
      throw new Error(`Pi manifest declares a missing extension file: ${ext}`);
    }
  }

  const entry = join(installedRoot, manifest.main);
  const adapter = await import(pathToFileURL(entry).href);
  if (typeof adapter.default !== "function" || typeof adapter.createPiExtension !== "function") {
    throw new Error("packed adapter factory exports are missing");
  }
  const observedEvents = [];
  adapter.default({ on: (event, handler) => {
    observedEvents.push(event);
    if (typeof handler !== "function") throw new Error("adapter registered an invalid handler");
  } });
  if (observedEvents.join(",") !== "session_start") {
    throw new Error("packed adapter registered an unexpected host surface");
  }
  await loadWithPi(entry);

  const nodeMajor = Number(process.versions.node.split(".")[0]);
  const nodeEngineSupported = nodeMajor >= 20 && nodeMajor < 23;
  console.log(JSON.stringify({
    package: `${manifest.name}@${manifest.version}`,
    packed_files: packedFiles.length,
    node_runtime: process.versions.node,
    node_engine_supported: nodeEngineSupported,
    factory_loaded: true,
    pi_host_loaded_packed_entry: true,
    built_in_tools_disabled: true,
    compatibility_claim: false,
  }));
} finally {
  rmSync(scratch, { recursive: true, force: true });
}
