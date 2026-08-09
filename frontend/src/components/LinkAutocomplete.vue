<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import type { LinkBinding } from "../runtime/link-autocomplete";
import { linkLabel, linkRecordName, linkSearchQuery } from "../runtime/link-autocomplete";
import type { ResourceClient } from "../runtime/resource-api";
const props = defineProps<{ binding: LinkBinding; client: ResourceClient; modelValue?: unknown; disabled?: boolean; error?: string }>();
const emit = defineEmits<{ "update:modelValue": [value: string] }>();
const search = ref(""); const records = ref<Readonly<Record<string, unknown>>[]>([]); const loading = ref(false); const loadError = ref<string | null>(null);
const selected = computed(() => records.value.find((record) => linkRecordName(record, props.binding) === props.modelValue));
async function load() { loading.value = true; loadError.value = null; try { const result = await props.client.list(props.binding.route, linkSearchQuery(props.binding, search.value)); records.value = Array.isArray((result as { data?: unknown[] }).data) ? ((result as { data: unknown[] }).data.filter((record): record is Record<string, unknown> => Boolean(record && typeof record === "object" && !Array.isArray(record)))) : []; } catch (cause) { loadError.value = cause instanceof Error ? cause.message : "Unable to load Link choices"; } finally { loading.value = false; } }
function choose(record: Readonly<Record<string, unknown>>) { const name = linkRecordName(record, props.binding); if (name) { emit("update:modelValue", name); search.value = ""; } }
onMounted(() => { void load(); });
</script>
<template><label><span>{{ binding.field.label }}<em v-if="binding.field.required"> *</em></span><input :disabled="disabled || binding.field.read_only" :value="search" :placeholder="selected ? linkLabel(selected, binding) : `Search ${binding.entity.label}`" @input="search = ($event.target as HTMLInputElement).value; void load()" /><small v-if="selected">Selected: {{ linkLabel(selected, binding) }}</small><p v-if="loading">Loading choices…</p><p v-else-if="loadError" role="alert">{{ loadError }}</p><ul v-else><li v-for="record in records" :key="linkRecordName(record, binding) ?? linkLabel(record, binding)"><button type="button" :disabled="disabled || binding.field.read_only" @click="choose(record)">{{ linkLabel(record, binding) }}</button></li></ul><small v-if="error" role="alert">{{ error }}</small></label></template>
