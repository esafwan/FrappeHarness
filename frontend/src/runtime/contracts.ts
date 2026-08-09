export type ActionSet = Readonly<{ read: boolean; create: boolean; write: boolean; delete: boolean }>;

export type FrontendField = Readonly<{
  name: string;
  label: string;
  field_type: string;
  required: boolean;
  unique: boolean;
  default: unknown;
  description: string;
  component: string;
  visible: true;
  read_only: boolean;
  select_options: readonly string[];
  link_target_entity: string | null;
  link_target: string | null;
  creatable: boolean;
  editable: boolean;
}>;

export type EntityManifest = Readonly<{
  name: string; doctype: string; label: string; description: string;
  fields: readonly FrontendField[]; actions: ActionSet;
}>;
export type QueryField = Readonly<{ name: string; field_type: string; operators: readonly string[] }>;
export type FrontendRoute = Readonly<{
  id: string; kind: "list" | "form"; entity: string; label: string; path: string;
  fields: readonly string[]; allowed_filters: readonly QueryField[]; allowed_orders: readonly string[];
  record_id_field: "name";
}>;
export type NavigationItem = Readonly<{ id: string; label: string; entity: string; route_id: string; path: string }>;
export type RoleFrontendManifest = Readonly<{
  role: string; entities: readonly EntityManifest[]; routes: readonly FrontendRoute[]; navigation: readonly NavigationItem[];
}>;
export type FrontendManifest = Readonly<{
  version: 1; target_frappe_major: 16; project: string; label: string; spec_hash: string;
  roles: readonly RoleFrontendManifest[]; manifest_hash: string;
}>;

/** Server-generated payload. `effective_role` is intentionally not a URL, UI, or caller argument. */
export type EffectiveManifestPayload = Readonly<{ effective_role: string; manifest: FrontendManifest }>;

export type EffectiveManifest = Readonly<{ role: RoleFrontendManifest; manifestHash: string; specHash: string }>;
