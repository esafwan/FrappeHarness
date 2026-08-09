<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import ListView from "./views/ListView.vue";
import RecordView from "./views/RecordView.vue";
import { HttpManifestSource, loadEffectiveManifest } from "./runtime/manifest-loader";
import { createResourceClient } from "./runtime/resource-api";
import { declaredNavigation, resolveRoute } from "./runtime/router";
import type { EffectiveManifest } from "./runtime/contracts";
const state = ref<EffectiveManifest | null>(null); const error = ref<string | null>(null); const pathname = ref(window.location.pathname); const creating = ref<string | null>(null);
const manifestEndpoint = (globalThis as { frappe_harness_manifest_endpoint?: unknown }).frappe_harness_manifest_endpoint;
const syncPath = () => { pathname.value = window.location.pathname; creating.value = null; };
onMounted(async () => { window.addEventListener("popstate", syncPath); try { const endpoint = typeof manifestEndpoint === "string" && manifestEndpoint.trim() ? manifestEndpoint : "/api/method/frappe_harness.api.frontend_manifest"; state.value = await loadEffectiveManifest(new HttpManifestSource(endpoint)); } catch (cause) { error.value = cause instanceof Error ? cause.message : "Unable to load application"; } });
onBeforeUnmount(() => window.removeEventListener("popstate", syncPath));
const current = computed(() => state.value ? resolveRoute(state.value.role, pathname.value) : null);
const formRoute = computed(() => creating.value && state.value ? state.value.role.routes.find((route) => route.entity === creating.value && route.kind === "form") ?? null : null);
const shownRoute = computed(() => formRoute.value ?? current.value?.route ?? null);
const entity = computed(() => shownRoute.value && state.value ? state.value.role.entities.find((candidate) => candidate.name === shownRoute.value!.entity) ?? null : null);
function navigate(path: string) { history.pushState({}, "", path); pathname.value = path; creating.value = null; }
function open(name: string) { if (!shownRoute.value || !state.value) return; const route = state.value.role.routes.find((candidate) => candidate.entity === shownRoute.value!.entity && candidate.kind === "form"); if (route) navigate(route.path.replace(":name", encodeURIComponent(name))); }
function create() { if (entity.value?.actions.create) creating.value = entity.value.name; }
function saved(name: string) { if (name) open(name); else creating.value = null; }
function deleted() { if (!shownRoute.value || !state.value) return; const route = state.value.role.routes.find((candidate) => candidate.entity === shownRoute.value!.entity && candidate.kind === "list"); if (route) navigate(route.path); }
</script>
<template><main><p v-if="error" role="alert">{{ error }}</p><p v-else-if="!state">Loading application…</p><template v-else><nav><a v-for="item in declaredNavigation(state.role)" :key="item.id" :href="item.path" @click.prevent="navigate(item.path)">{{ item.label }}</a></nav><p v-if="!shownRoute || !entity">Page not found.</p><ListView v-else-if="shownRoute.kind === 'list'" :entity="entity" :route="shownRoute" :client="createResourceClient(entity, undefined, { requireCsrf: true })" @open="open" @create="create" /><RecordView v-else :key="creating ?? current?.params.name ?? 'create'" :entity="entity" :route="shownRoute" :role="state.role" :name="creating ? undefined : current?.params.name" :client="createResourceClient(entity, undefined, { requireCsrf: true })" @saved="saved" @deleted="deleted" @cancel="creating = null" /></template></main></template>
