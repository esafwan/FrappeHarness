import { describe, expect, it } from "vitest";
import { isDirty, makeDraft, mutationFields, mutationPayload, RecordDraftError, validateDraft } from "../src/runtime/record-draft";
import { fixtureManifest } from "./fixture";

const role = fixtureManifest().roles[0]; const entity = role.entities[0]; const form = role.routes[1];
describe("record drafts", () => {
  it("permits only manifest-approved form mutation fields", () => {
    const fields = mutationFields(entity, form, "create");
    expect(makeDraft(entity, form, "create")).toEqual({ title: null });
    expect(mutationPayload(fields, { title: "Golden", injected: "no" })).toEqual({ title: "Golden" });
  });
  it("rejects incomplete drafts and a permissionless mode", () => {
    const fields = mutationFields(entity, form, "create");
    expect(validateDraft(fields, { title: "" })).toEqual({ title: "Title is required" });
    expect(() => mutationPayload(fields, {})).toThrow(RecordDraftError);
    expect(isDirty({ title: "A" }, { title: "B" })).toBe(true);
  });
});
