import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";
import LinkAutocomplete from "../src/components/LinkAutocomplete.vue";
import { linkLabel, linkSearchQuery, resolveLinkBinding } from "../src/runtime/link-autocomplete";
import type { ResourceClient } from "../src/runtime/resource-api";
import { linkRole } from "./fixture";

const role = linkRole(); const field = role.entities[0].fields[0]; const binding = resolveLinkBinding(role, field);
const client: ResourceClient = { list: vi.fn(async () => ({ data: [{ name: "PROJ-1", title: "Golden project" }] })), read: vi.fn(), create: vi.fn(), update: vi.fn(), delete: vi.fn() };
describe("manifest-bound Link autocomplete", () => {
  it("uses the readable target/list route and only its declared contains filter", () => {
    expect(binding.entity.name).toBe("project"); expect(linkSearchQuery(binding, "golden")).toMatchObject({ filters: [{ field: "title", operator: "contains", value: "golden" }] }); expect(linkLabel({ name: "PROJ-1", title: "Golden project" }, binding)).toBe("Golden project (PROJ-1)");
  });
  it("renders readable labels and emits only a returned declared record id", async () => {
    const wrapper = mount(LinkAutocomplete, { props: { binding, client } }); await flushPromises(); expect(wrapper.text()).toContain("Golden project (PROJ-1)"); await wrapper.get("button").trigger("click"); expect(wrapper.emitted("update:modelValue")).toEqual([["PROJ-1"]]); await wrapper.get("input").setValue("golden"); await flushPromises(); expect(client.list).toHaveBeenLastCalledWith(binding.route, expect.objectContaining({ filters: [{ field: "title", operator: "contains", value: "golden" }] }));
  });
});
