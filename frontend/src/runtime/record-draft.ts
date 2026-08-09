import type { EntityManifest, FrontendField, FrontendRoute } from "./contracts";

export type RecordValues = Readonly<Record<string, unknown>>;
export type RecordMode = "create" | "edit";
export type ValidationErrors = Readonly<Record<string, string>>;
export class RecordDraftError extends Error {}

const layouts = new Set(["form-section", "column-break"]);
const empty = (value: unknown) => value === undefined || value === null || value === "";

export function mutationFields(entity: EntityManifest, route: FrontendRoute, mode: RecordMode): readonly FrontendField[] {
  if (route.kind !== "form" || route.entity !== entity.name) throw new RecordDraftError("Record mutations require the entity's declared form route");
  if (mode === "create" && !entity.actions.create) throw new RecordDraftError("Create is not permitted by the effective manifest");
  if (mode === "edit" && !entity.actions.write) throw new RecordDraftError("Update is not permitted by the effective manifest");
  const names = new Set(route.fields);
  return entity.fields.filter((field) => names.has(field.name) && !layouts.has(field.component) && (mode === "create" ? field.creatable : field.editable));
}

export function makeDraft(entity: EntityManifest, route: FrontendRoute, mode: RecordMode, record: RecordValues = {}): Record<string, unknown> {
  return Object.fromEntries(mutationFields(entity, route, mode).map((field) => [field.name, record[field.name] ?? field.default]));
}

export function validateDraft(fields: readonly FrontendField[], values: RecordValues): ValidationErrors {
  return Object.fromEntries(fields.filter((field) => field.required && empty(values[field.name])).map((field) => [field.name, `${field.label} is required`]));
}

export function mutationPayload(fields: readonly FrontendField[], values: RecordValues): Record<string, unknown> {
  const errors = validateDraft(fields, values);
  if (Object.keys(errors).length) throw new RecordDraftError("Required fields are missing");
  return Object.fromEntries(fields.filter((field) => values[field.name] !== undefined).map((field) => [field.name, values[field.name]]));
}

export function isDirty(initial: RecordValues, current: RecordValues): boolean {
  const keys = new Set([...Object.keys(initial), ...Object.keys(current)]);
  return [...keys].some((key) => JSON.stringify(initial[key]) !== JSON.stringify(current[key]));
}
