import type { FrontendRoute, RoleFrontendManifest } from "./contracts";

export type ResolvedRoute = Readonly<{ route: FrontendRoute; params: Readonly<Record<string, string>> }>;

export function declaredNavigation(manifest: RoleFrontendManifest) {
  const routeById = new Map(manifest.routes.map((route) => [route.id, route]));
  return manifest.navigation.map((item) => ({ ...item, route: routeById.get(item.route_id)! }));
}

export function resolveRoute(manifest: RoleFrontendManifest, pathname: string): ResolvedRoute | null {
  const input = pathname.replace(/\/+$/, "") || "/";
  for (const route of manifest.routes) {
    const names: string[] = [];
    const pattern = `^${route.path.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/:([A-Za-z][A-Za-z0-9_]*)/g, (_, name: string) => { names.push(name); return "([^/]+)"; })}$`;
    const match = new RegExp(pattern).exec(input);
    if (match) return { route, params: Object.freeze(Object.fromEntries(names.map((name, index) => [name, decodeURIComponent(match[index + 1])]))) };
  }
  return null;
}
