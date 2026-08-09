import type { EntityManifest, FrontendField, FrontendRoute, QueryField, RoleFrontendManifest } from "./contracts";
import type { ListQuery } from "./resource-api";

export type LinkBinding = Readonly<{ field: FrontendField; entity: EntityManifest; route: FrontendRoute; labelField: string; searchFilter?: QueryField }>;
export class LinkBindingError extends Error {}

/** Resolve a Link strictly within the already role-scoped manifest. */
export function resolveLinkBinding(role: RoleFrontendManifest, field: FrontendField): LinkBinding {
  if (field.component !== "link-autocomplete" || !field.link_target_entity || !field.link_target) throw new LinkBindingError("Field is not a declared Link");
  const entity = role.entities.find((candidate) => candidate.name === field.link_target_entity);
  const route = role.routes.find((candidate) => candidate.entity === field.link_target_entity && candidate.kind === "list");
  if (!entity || !entity.actions.read || !route) throw new LinkBindingError("Link target is not readable in the effective manifest");
  const labelField = route.fields[0];
  if (!labelField) throw new LinkBindingError("Link target needs a declared list label field");
  return { field, entity, route, labelField, searchFilter: route.allowed_filters.find((candidate) => candidate.operators.includes("contains")) };
}

/** The only remote search filter is a manifest-declared contains field. */
export function linkSearchQuery(binding: LinkBinding, search: string): ListQuery {
  const value = search.trim();
  return { filters: value && binding.searchFilter ? [{ field: binding.searchFilter.name, operator: "contains", value }] : [], limitStart: 0, limitPageLength: 20 };
}

export function linkRecordName(record: Readonly<Record<string, unknown>>, binding: LinkBinding): string | null {
  const value = record[binding.route.record_id_field];
  return typeof value === "string" && value ? value : null;
}

export function linkLabel(record: Readonly<Record<string, unknown>>, binding: LinkBinding): string {
  const name = linkRecordName(record, binding) ?? "";
  const label = record[binding.labelField];
  return typeof label === "string" && label ? (label === name ? label : `${label} (${name})`) : name;
}
