import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";

const manifest = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
const nodeMajor = Number(process.versions.node.split(".")[0]);
const observedPi = spawnSync("pi", ["--version"], {
  encoding: "utf8",
  env: { ...process.env, PI_OFFLINE: "1" },
});
const reasons = [];

if (!(nodeMajor >= 20 && nodeMajor < 23)) reasons.push("unsupported_node_runtime");
if (observedPi.error || observedPi.status !== 0 || observedPi.stdout.trim() !== manifest.piHostVersion) {
  reasons.push("pi_host_version_mismatch");
}
reasons.push("host_version_negotiation_absent", "model_backed_session_not_evidenced");

console.error(JSON.stringify({
  passed: false,
  compatibility_claim: false,
  node_runtime: process.versions.node,
  declared_node_engine: manifest.engines.node,
  observed_pi: observedPi.stdout?.trim() || null,
  reasons,
}));
process.exitCode = 1;
