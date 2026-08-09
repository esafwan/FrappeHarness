import { describe, expect, it } from "vitest";
import { declaredNavigation, resolveRoute } from "../src/runtime/router";
import { fixtureManifest } from "./fixture";
const role = fixtureManifest().roles[0];
describe("declared routes", () => {
  it("resolves only confirmed routes", () => { expect(resolveRoute(role, "/task_tracker/task/ABC")).toMatchObject({ route: { id: "task_form" }, params: { name: "ABC" } }); expect(resolveRoute(role, "/task_tracker/unlisted")).toBeNull(); });
  it("uses only declared navigation", () => expect(declaredNavigation(role).map((item) => item.route.id)).toEqual(["task_list"]));
});
