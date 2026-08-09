import { describe, expect, it, vi } from "vitest";
import { buildListQuery, createResourceClient, ResourceRequestError } from "../src/runtime/resource-api";
import { fixtureManifest } from "./fixture";
const fixture = fixtureManifest(); const route = fixture.roles[0].routes[0];
describe("fixed resource query projection", () => {
  it("serializes declared list fields plus the explicit record identifier", () => { const result = buildListQuery(route, { filters: [{ field: "title", operator: "contains", value: "golden" }], order: "title:asc", limitPageLength: 20 }); expect(result.get("fields")).toBe('["title","name"]'); expect(result.get("order_by")).toBe("title asc"); });
  it("rejects unknown fields, order, and excessive page sizes", () => { expect(() => buildListQuery(route, { filters: [{ field: "owner", operator: "equals", value: "x" }] })).toThrow(ResourceRequestError); expect(() => buildListQuery(route, { order: "owner:asc" })).toThrow(ResourceRequestError); expect(() => buildListQuery(route, { limitPageLength: 101 })).toThrow(ResourceRequestError); });
  it("uses fixed resource methods and normalizes mutation failures", async () => {
    const requestMock = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({ data: { name: "TASK-1" } }), { status: 200, headers: { "Content-Type": "application/json" } }));
    const client = createResourceClient(fixture.roles[0].entities[0], requestMock as unknown as typeof fetch, { csrfToken: "csrf-test" });
    await client.create({ title: "Golden" }); await client.update("TASK-1", { title: "Changed" });
    expect(requestMock.mock.calls.map(([url, init]) => [url, (init as RequestInit | undefined)?.method, (init as RequestInit | undefined)?.body])).toEqual([["/api/resource/Task", "POST", '{"title":"Golden"}'], ["/api/resource/Task/TASK-1", "PUT", '{"title":"Changed"}']]);
    expect(requestMock.mock.calls.every(([, init]) => (init as RequestInit).credentials === "same-origin")).toBe(true);
    expect((requestMock.mock.calls[0][1] as RequestInit).headers).toMatchObject({ Accept: "application/json", "Content-Type": "application/json", "X-Frappe-CSRF-Token": "csrf-test" });
    const denied = createResourceClient(fixture.roles[0].entities[0], (async () => new Response(JSON.stringify({ message: "Denied", field_errors: { title: "Nope" } }), { status: 403, headers: { "Content-Type": "application/json" } })) as typeof fetch);
    await expect(denied.create({ title: "Golden" })).rejects.toMatchObject({ message: "Denied", status: 403, fieldErrors: { title: "Nope" } });
    const manager = { ...fixture.roles[0].entities[0], actions: { ...fixture.roles[0].entities[0].actions, delete: true } };
    const deleteClient = createResourceClient(manager, requestMock as unknown as typeof fetch); await deleteClient.delete("TASK-1");
    expect(requestMock).toHaveBeenLastCalledWith("/api/resource/Task/TASK-1", expect.objectContaining({ method: "DELETE" }));
    await expect(createResourceClient(fixture.roles[0].entities[0], requestMock as unknown as typeof fetch).delete("TASK-1")).rejects.toMatchObject({ message: "Delete is not permitted by the effective manifest" });
  });
  it("requires CSRF only when the production adapter opts in", async () => {
    const requestMock = vi.fn(async () => new Response(JSON.stringify({ data: {} }), { status: 200 }));
    const client = createResourceClient(fixture.roles[0].entities[0], requestMock as unknown as typeof fetch, { requireCsrf: true });
    await expect(client.create({ title: "Golden" })).rejects.toMatchObject({ message: "Frappe CSRF token is unavailable" });
    expect(requestMock).not.toHaveBeenCalled();
    await expect(client.list(route)).resolves.toEqual({ data: {} });
  });
});
