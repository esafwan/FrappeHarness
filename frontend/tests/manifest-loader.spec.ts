import { createHash } from "node:crypto";
import { describe, expect, it } from "vitest";
import { canonicalJson, loadEffectiveManifest, ManifestError, validateManifest } from "../src/runtime/manifest-loader";
import { fixtureManifest } from "./fixture";
const hash = async (value: string) => createHash("sha256").update(value).digest("hex");
describe("manifest loader", () => {
  it("validates the compiler-compatible canonical hash", async () => expect(await validateManifest(fixtureManifest(), hash)).toMatchObject({ project: "task_tracker" }));
  it("rejects a modified manifest", async () => { const altered = { ...fixtureManifest(), label: "Altered" }; await expect(validateManifest(altered, hash)).rejects.toBeInstanceOf(ManifestError); });
  it("does not permit a caller-selected role", async () => { const manifest = fixtureManifest(); const source = { load: async () => ({ effective_role: "Task User", manifest }) }; await expect(loadEffectiveManifest(source, hash)).resolves.toMatchObject({ role: { role: "Task User" } }); });
  it("canonicalizes object keys deterministically", () => expect(canonicalJson({ b: 1, a: [true, null] })).toBe('{"a":[true,null],"b":1}'));
});
