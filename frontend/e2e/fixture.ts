import { createHash } from "node:crypto";
import type { Page } from "@playwright/test";
import type { FrontendManifest, RoleFrontendManifest } from "../src/runtime/contracts";

type Mode = "user" | "manager";
type DeleteMode = "success" | "denied";
export type FakeApi = { requests: Array<{ method: string; url: URL; body: unknown; csrf?: string }>; deleteMode?: DeleteMode; empty?: boolean; delayList?: boolean };

// Kept local so Playwright's Node test runner does not import Vue SFC modules.
const canonicalJson = (value: unknown): string => {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
};

const field = (name: string, label: string, fieldType: string, component: string, extra: Record<string, unknown> = {}) => ({ name, label, field_type: fieldType, required: name === "title" || name === "project" || name === "status", unique: false, default: name === "status" ? "Open" : null, description: "", component, visible: true as const, read_only: false, select_options: name === "status" ? ["Open", "In Progress", "Completed"] : [], link_target_entity: name === "project" ? "project" : null, link_target: name === "project" ? "Project" : null, creatable: true, editable: true, ...extra });

function role(mode: Mode): RoleFrontendManifest {
  const manager = mode === "manager";
  return {
    role: manager ? "Task Manager" : "Task User",
    entities: [
      { name: "project", doctype: "Project", label: "Project", description: "", fields: [field("title", "Title", "Data", "text-input")], actions: { read: true, create: manager, write: manager, delete: manager } },
      { name: "task", doctype: "Task", label: "Task", description: "", fields: [field("title", "Subject", "Data", "text-input"), field("project", "Project", "Link", "link-autocomplete"), field("status", "Status", "Select", "select")], actions: { read: true, create: true, write: true, delete: manager } },
    ],
    routes: [
      { id: "project_list", kind: "list", entity: "project", label: "Projects", path: "/task_tracker/project", fields: ["title"], allowed_filters: [{ name: "title", field_type: "Data", operators: ["equals", "contains"] }], allowed_orders: ["title:asc", "title:desc"], record_id_field: "name" },
      { id: "project_form", kind: "form", entity: "project", label: "Project", path: "/task_tracker/project/:name", fields: ["title"], allowed_filters: [], allowed_orders: [], record_id_field: "name" },
      { id: "task_list", kind: "list", entity: "task", label: "Tasks", path: "/task_tracker/task", fields: ["title", "project", "status"], allowed_filters: [{ name: "title", field_type: "Data", operators: ["equals", "contains"] }, { name: "status", field_type: "Select", operators: ["equals", "in"] }], allowed_orders: ["title:asc", "title:desc"], record_id_field: "name" },
      { id: "task_form", kind: "form", entity: "task", label: "Task", path: "/task_tracker/task/:name", fields: ["title", "project", "status"], allowed_filters: [], allowed_orders: [], record_id_field: "name" },
    ],
    navigation: [{ id: "project_list", label: "Projects", entity: "project", route_id: "project_list", path: "/task_tracker/project" }, { id: "task_list", label: "Tasks", entity: "task", route_id: "task_list", path: "/task_tracker/task" }],
  };
}

function manifest(mode: Mode): FrontendManifest {
  const base = { version: 1 as const, target_frappe_major: 16 as const, project: "task_tracker", label: "Task Tracker", spec_hash: "a".repeat(64), roles: [role(mode)] };
  return { ...base, manifest_hash: createHash("sha256").update(canonicalJson(base)).digest("hex") };
}

export async function installFakeFrappe(page: Page, mode: Mode, api: FakeApi): Promise<void> {
  const current = manifest(mode);
  // The production runtime deliberately refuses mutations without a Frappe
  // CSRF token. Keep the fake browser contract honest by supplying the same
  // server-derived global that Frappe exposes before the first navigation.
  await page.addInitScript(() => { (globalThis as { csrf_token?: string }).csrf_token = "e2e-csrf-token"; });
  await page.route("**/api/method/frappe_harness.api.frontend_manifest", async (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify({ effective_role: current.roles[0].role, manifest: current }) }));
  await page.route("**/api/resource/**", async (route) => {
    const request = route.request(); const url = new URL(request.url()); const method = request.method(); const body = request.postDataJSON() as unknown; api.requests.push({ method, url, body, csrf: request.headers()["x-frappe-csrf-token"] });
    if (method === "GET" && url.pathname === "/api/resource/Project") return route.fulfill({ contentType: "application/json", body: JSON.stringify({ data: [{ name: "PROJ-1", title: "Golden Project" }] }) });
    if (method === "GET" && url.pathname === "/api/resource/Task") { if (api.delayList) await new Promise((resolve) => setTimeout(resolve, 120)); return route.fulfill({ contentType: "application/json", body: JSON.stringify({ data: api.empty ? [] : [{ name: "TASK-1", title: "Golden Task", project: "PROJ-1", status: "Open" }] }) }); }
    if (method === "GET" && url.pathname.startsWith("/api/resource/Task/")) return route.fulfill({ contentType: "application/json", body: JSON.stringify({ data: { name: url.pathname.split("/").at(-1), title: "Golden Task", project: "PROJ-1", status: "Open" } }) });
    if (method === "POST") return route.fulfill({ contentType: "application/json", body: JSON.stringify({ data: { name: "TASK-NEW" } }) });
    if (method === "PUT") return route.fulfill({ contentType: "application/json", body: JSON.stringify({ data: { name: "TASK-1" } }) });
    if (method === "DELETE" && api.deleteMode === "denied") return route.fulfill({ status: 403, contentType: "application/json", body: JSON.stringify({ message: "Permission denied" }) });
    if (method === "DELETE") return route.fulfill({ contentType: "application/json", body: JSON.stringify({ data: {} }) });
    return route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ message: "Missing" }) });
  });
}
