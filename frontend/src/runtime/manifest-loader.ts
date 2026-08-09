import { COMPONENT_NAMES } from "./field-registry";
import type { EffectiveManifest, EffectiveManifestPayload, FrontendManifest, RoleFrontendManifest } from "./contracts";

export class ManifestError extends Error {}
export type HashFunction = (value: string) => Promise<string>;

/** Mirrors the compiler's sorted, compact canonical JSON encoding. */
export function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
}

export async function sha256Hex(value: string): Promise<string> {
  const bytes = new TextEncoder().encode(value);
  const hash = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

const digest = /^[0-9a-f]{64}$/;
const identifier = /^[a-z][a-z0-9_]*$/;
const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const strings = (value: unknown): value is string[] => Array.isArray(value) && value.every((item) => typeof item === "string");

export async function validateManifest(value: unknown, hash: HashFunction = sha256Hex): Promise<FrontendManifest> {
  if (!isRecord(value) || value.version !== 1 || value.target_frappe_major !== 16 || !Array.isArray(value.roles)) throw new ManifestError("Invalid V1 frontend manifest");
  const manifest = value as unknown as FrontendManifest;
  if (typeof manifest.project !== "string" || typeof manifest.label !== "string" || !digest.test(manifest.spec_hash) || !digest.test(manifest.manifest_hash)) throw new ManifestError("Manifest identifiers or hashes are invalid");
  if (!Array.isArray(manifest.roles) || manifest.roles.length === 0) throw new ManifestError("Manifest must contain role projections");
  const seen = new Set<string>();
  for (const role of manifest.roles) validateRole(role, seen);
  const { manifest_hash: suppliedHash, ...unhashed } = manifest;
  if (await hash(canonicalJson(unhashed)) !== suppliedHash) throw new ManifestError("Manifest hash mismatch");
  return manifest;
}

function validateRole(role: RoleFrontendManifest, seen: Set<string>): void {
  if (!role || typeof role.role !== "string" || !role.role || seen.has(role.role) || !Array.isArray(role.entities) || !Array.isArray(role.routes) || !Array.isArray(role.navigation)) throw new ManifestError("Invalid role projection");
  seen.add(role.role);
  if (role.entities.some((entity) => !isRecord(entity)) || role.routes.some((route) => !isRecord(route)) || role.navigation.some((item) => !isRecord(item))) throw new ManifestError("Role projection members must be objects");
  const entities = new Map(role.entities.map((entity) => [entity.name, entity]));
  if (entities.size !== role.entities.length) throw new ManifestError("Duplicate entity in role projection");
  for (const entity of role.entities) {
    if (!identifier.test(entity.name) || typeof entity.doctype !== "string" || !entity.actions || !Array.isArray(entity.fields)) throw new ManifestError("Invalid entity projection");
    for (const action of [entity.actions.read, entity.actions.create, entity.actions.write, entity.actions.delete]) if (typeof action !== "boolean") throw new ManifestError("Invalid entity actions");
    for (const field of entity.fields) {
      if (!isRecord(field) || !identifier.test(field.name as string) || typeof field.label !== "string" || field.visible !== true || !COMPONENT_NAMES.includes(field.component as string) || !strings(field.select_options)) throw new ManifestError("Invalid frontend field component");
    }
  }
  const routes = new Map(role.routes.map((route) => [route.id, route]));
  if (routes.size !== role.routes.length) throw new ManifestError("Duplicate route identifier");
  for (const route of role.routes) {
    if (!entities.has(route.entity) || (route.kind !== "list" && route.kind !== "form") || typeof route.path !== "string" || !strings(route.fields) || !Array.isArray(route.allowed_filters) || !strings(route.allowed_orders) || route.record_id_field !== "name") throw new ManifestError("Invalid route projection");
    const fields = new Set(route.fields);
    for (const query of route.allowed_filters) if (!isRecord(query) || !fields.has(query.name as string) || typeof query.field_type !== "string" || !strings(query.operators) || !query.operators.length) throw new ManifestError("Invalid route filter projection");
    for (const order of route.allowed_orders) if (!fields.has(order.split(":", 1)[0]) || !/(?:asc|desc)$/.test(order)) throw new ManifestError("Invalid route order projection");
  }
  for (const item of role.navigation) if (!item || typeof item.id !== "string" || typeof item.path !== "string" || !routes.has(item.route_id) || !entities.has(item.entity)) throw new ManifestError("Navigation must reference a declared route and entity");
}

export interface ManifestSource { load(): Promise<EffectiveManifestPayload>; }

/** A same-origin Frappe endpoint must derive the role from the authenticated session. */
export class HttpManifestSource implements ManifestSource {
  constructor(private readonly endpoint: string, private readonly request: typeof fetch = (...args) => globalThis.fetch(...args)) {}
  async load(): Promise<EffectiveManifestPayload> {
    const response = await this.request(this.endpoint, { credentials: "same-origin", headers: { Accept: "application/json" } });
    if (!response.ok) throw new ManifestError(`Manifest request failed (${response.status})`);
    const payload = await response.json() as unknown;
    // Frappe wraps whitelisted method returns in {message: ...}; local
    // contract fixtures may return the same object directly.
    if (isRecord(payload) && isRecord(payload.message)) return payload.message as EffectiveManifestPayload;
    return payload as EffectiveManifestPayload;
  }
}

export async function loadEffectiveManifest(source: ManifestSource, hash?: HashFunction): Promise<EffectiveManifest> {
  const payload = await source.load();
  if (!isRecord(payload) || typeof payload.effective_role !== "string") throw new ManifestError("Server did not supply an effective role");
  const manifest = await validateManifest(payload.manifest, hash);
  const role = manifest.roles.find((candidate) => candidate.role === payload.effective_role);
  if (!role) throw new ManifestError("Server effective role is not present in manifest");
  return Object.freeze({ role, manifestHash: manifest.manifest_hash, specHash: manifest.spec_hash });
}
