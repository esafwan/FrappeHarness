import type { EntityManifest, FrontendRoute } from "./contracts";

export type QueryFilter = Readonly<{ field: string; operator: string; value: string | number | boolean | readonly string[] }>;
export type ListQuery = Readonly<{ filters?: readonly QueryFilter[]; order?: string; limitStart?: number; limitPageLength?: number }>;
export class ResourceRequestError extends Error {
  constructor(message: string, readonly status?: number, readonly fieldErrors: Readonly<Record<string, string>> = {}) { super(message); }
}

export function buildListQuery(route: FrontendRoute, query: ListQuery = {}): URLSearchParams {
  if (route.kind !== "list") throw new ResourceRequestError("Only list routes accept list queries");
  const filters = query.filters ?? [];
  if (filters.some((filter) => !route.allowed_filters.some((allowed) => allowed.name === filter.field && allowed.operators.includes(filter.operator)))) throw new ResourceRequestError("Unapproved filter");
  if (query.order && !route.allowed_orders.includes(query.order)) throw new ResourceRequestError("Unapproved ordering");
  if (query.limitStart !== undefined && (!Number.isInteger(query.limitStart) || query.limitStart < 0)) throw new ResourceRequestError("Invalid list offset");
  if (query.limitPageLength !== undefined && (!Number.isInteger(query.limitPageLength) || query.limitPageLength < 1 || query.limitPageLength > 100)) throw new ResourceRequestError("Invalid list limit");
  const fields = [...new Set([...route.fields, route.record_id_field])];
  const params = new URLSearchParams({ fields: JSON.stringify(fields) });
  if (filters.length) params.set("filters", JSON.stringify(filters.map((filter) => [filter.field, filter.operator, filter.value])));
  if (query.order) params.set("order_by", query.order.replace(":", " "));
  if (query.limitStart !== undefined) params.set("limit_start", String(query.limitStart));
  if (query.limitPageLength !== undefined) params.set("limit_page_length", String(query.limitPageLength));
  return params;
}

export interface ResourceClient {
  list(route: FrontendRoute, query?: ListQuery): Promise<unknown>;
  read(name: string): Promise<unknown>;
  create(values: Readonly<Record<string, unknown>>): Promise<unknown>;
  update(name: string, values: Readonly<Record<string, unknown>>): Promise<unknown>;
  delete(name: string): Promise<unknown>;
}
export type ResourceClientOptions = Readonly<{
  csrfToken?: string | (() => string | undefined);
  requireCsrf?: boolean;
}>;

export function createResourceClient(
  entity: EntityManifest,
  request: typeof fetch = (...args) => globalThis.fetch(...args),
  options: ResourceClientOptions = {},
): ResourceClient {
  const base = `/api/resource/${encodeURIComponent(entity.doctype)}`;
  const call = async (path: string, method: "GET" | "POST" | "PUT" | "DELETE" = "GET", values?: Readonly<Record<string, unknown>>): Promise<unknown> => {
    const headers: Record<string, string> = { Accept: "application/json" };
    const mutating = method !== "GET";
    if (mutating) {
      const csrfToken = typeof options.csrfToken === "function" ? options.csrfToken() : options.csrfToken ?? defaultCsrfToken();
      if (options.requireCsrf && !csrfToken) throw new ResourceRequestError("Frappe CSRF token is unavailable");
      if (csrfToken) headers["X-Frappe-CSRF-Token"] = csrfToken;
      headers["Content-Type"] = "application/json";
    }
    const response = await request(path, {
      method,
      credentials: "same-origin",
      headers,
      ...(values === undefined ? {} : { body: JSON.stringify(values) }),
    });
    const payload = await response.json().catch(() => null) as unknown;
    if (!response.ok) throw normalizedError(response.status, payload);
    return payload;
  };
  const mutation = (method: "POST" | "PUT", path: string, values: Readonly<Record<string, unknown>>) => call(path, method, values);
  return {
    list: (route, query) => call(`${base}?${buildListQuery(route, query)}`),
    read: (name) => call(`${base}/${encodeURIComponent(name)}`),
    create: async (values) => { if (!entity.actions.create) throw new ResourceRequestError("Create is not permitted by the effective manifest"); return mutation("POST", base, values); },
    update: async (name, values) => { if (!entity.actions.write) throw new ResourceRequestError("Update is not permitted by the effective manifest"); return mutation("PUT", `${base}/${encodeURIComponent(name)}`, values); },
    delete: async (name) => { if (!entity.actions.delete) throw new ResourceRequestError("Delete is not permitted by the effective manifest"); return call(`${base}/${encodeURIComponent(name)}`, "DELETE"); },
  };
}

function defaultCsrfToken(): string | undefined {
  if (typeof document !== "undefined") {
    const content = document.querySelector('meta[name="csrf-token"]')?.getAttribute("content");
    if (content?.trim()) return content;
  }
  const globalToken = (globalThis as { csrf_token?: unknown }).csrf_token;
  return typeof globalToken === "string" && globalToken.trim() ? globalToken : undefined;
}

function normalizedError(status: number, payload: unknown): ResourceRequestError {
  const body = payload && typeof payload === "object" ? payload as Record<string, unknown> : {};
  const message = stringMessage(body.message) ?? stringMessage(body.exception) ?? `Frappe resource request failed (${status})`;
  const fieldErrors = body.field_errors && typeof body.field_errors === "object" && !Array.isArray(body.field_errors)
    ? Object.fromEntries(Object.entries(body.field_errors as Record<string, unknown>).filter((entry): entry is [string, string] => typeof entry[1] === "string")) : {};
  return new ResourceRequestError(message, status, fieldErrors);
}
function stringMessage(value: unknown): string | undefined { return typeof value === "string" && value.trim() ? value : undefined; }
