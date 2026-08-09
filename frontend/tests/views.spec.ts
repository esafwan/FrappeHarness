import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";
import ListView from "../src/views/ListView.vue";
import RecordView from "../src/views/RecordView.vue";
import { ResourceRequestError, type ResourceClient } from "../src/runtime/resource-api";
import { fixtureManifest } from "./fixture";

const fixture = fixtureManifest(); const entity = fixture.roles[0].entities[0]; const list = fixture.roles[0].routes[0]; const form = fixture.roles[0].routes[1];
const client = (overrides: Partial<ResourceClient> = {}): ResourceClient => ({ list: vi.fn(async () => ({ data: [] })), read: vi.fn(async () => ({ data: {} })), create: vi.fn(async () => ({ data: { name: "TASK-1" } })), update: vi.fn(async () => ({ data: { name: "TASK-1" } })), delete: vi.fn(async () => ({ data: {} })), ...overrides });
describe("generic views", () => {
  it("renders declared list values, opens the declared record id, and emits create", async () => {
    const resource = client({ list: vi.fn(async () => ({ data: [{ name: "TASK-1", title: "Golden" }] })) }); const wrapper = mount(ListView, { props: { entity, route: list, client: resource } }); await flushPromises();
    expect(wrapper.text()).toContain("Golden"); await wrapper.get("tbody button").trigger("click"); await wrapper.get("button").trigger("click"); expect(wrapper.emitted("open")).toEqual([["TASK-1"]]); expect(wrapper.emitted("create")).toHaveLength(1);
  });
  it("does not submit an invalid create and submits only after required input", async () => {
    const resource = client(); const wrapper = mount(RecordView, { props: { entity, route: form, role: fixture.roles[0], client: resource } }); await wrapper.get("form").trigger("submit"); expect(resource.create).not.toHaveBeenCalled(); expect(wrapper.text()).toContain("Title is required"); await wrapper.get("input").setValue("Golden"); await wrapper.get("form").trigger("submit"); await flushPromises(); expect(resource.create).toHaveBeenCalledWith({ title: "Golden" }); expect(wrapper.emitted("saved")).toEqual([["TASK-1"]]);
  });
  it("loads and updates an existing record", async () => {
    const resource = client({ read: vi.fn(async () => ({ data: { title: "Old" } })), update: vi.fn(async () => ({ data: { name: "TASK-1" } })) }); const wrapper = mount(RecordView, { props: { entity, route: form, role: fixture.roles[0], name: "TASK-1", client: resource } }); await flushPromises(); expect((wrapper.get("input").element as HTMLInputElement).value).toBe("Old"); await wrapper.get("input").setValue("New"); await wrapper.get("form").trigger("submit"); await flushPromises(); expect(resource.update).toHaveBeenCalledWith("TASK-1", { title: "New" });
  });
  it("shows delete only for a permitted edit, requires confirmation, and reports denial", async () => {
    const manager = { ...entity, actions: { ...entity.actions, delete: true } }; const role = { ...fixture.roles[0], entities: [manager] };
    const resource = client({ read: vi.fn(async () => ({ data: { title: "Old" } })), delete: vi.fn(async () => { throw new ResourceRequestError("Denied", 403); }) }); const wrapper = mount(RecordView, { props: { entity: manager, route: form, role, name: "TASK-1", client: resource } }); await flushPromises();
    expect(wrapper.text()).toContain("Delete"); await wrapper.get("button:nth-of-type(2)").trigger("click"); expect(wrapper.get('[role="alertdialog"]').text()).toContain("cannot be undone"); await wrapper.get('[role="alertdialog"] button').trigger("click"); await flushPromises(); expect(resource.delete).toHaveBeenCalledWith("TASK-1"); expect(wrapper.text()).toContain("Denied"); expect(wrapper.emitted("deleted")).toBeUndefined();
    const user = mount(RecordView, { props: { entity, route: form, role: fixture.roles[0], name: "TASK-1", client: client() } }); await flushPromises(); expect(user.text()).not.toContain("Delete");
  });
});
