import { spawnSync } from "node:child_process";

const PINNED_PI_VERSION = "0.82.1";

function run(args) {
  const result = spawnSync("pi", args, {
    encoding: "utf8",
    env: { ...process.env, PI_OFFLINE: "1" },
    timeout: 10000,
  });
  if (result.error || result.status !== 0) throw new Error("Pi host command failed");
  return result.stdout;
}

const version = run(["--version"]).trim();
if (version !== PINNED_PI_VERSION) throw new Error("Pi host version does not match the pinned compatibility observation");
const help = run(["--help"]);
const requiredFlags = ["--no-session", "--no-tools", "--no-extensions", "--no-skills", "--offline"];
if (requiredFlags.some((flag) => !help.includes(flag))) throw new Error("required isolation flag missing");
run([...requiredFlags, "--help"]);
console.log(JSON.stringify({ observed_version: version, required_isolation_flags: requiredFlags,
  flags_present: true, isolated_startup_exit: 0, model_session_started: false,
  network_enabled: false, compatibility_claim: false }, null, 2));
