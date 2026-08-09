<script setup lang="ts">
import type { FrontendField } from "../runtime/contracts";
defineProps<{ field: FrontendField; modelValue?: unknown; disabled?: boolean; error?: string }>();
defineEmits<{ "update:modelValue": [value: unknown] }>();
</script>

<template>
  <section v-if="field.component === 'form-section'" class="form-section"><h2>{{ field.label }}</h2></section>
  <span v-else-if="field.component === 'column-break'" class="column-break" aria-hidden="true" />
  <label v-else>
    <span>{{ field.label }}<em v-if="field.required"> *</em></span>
    <select v-if="field.component === 'select'" :disabled="disabled || field.read_only" :value="modelValue" @change="$emit('update:modelValue', ($event.target as HTMLSelectElement).value)">
      <option v-for="option in field.select_options" :key="option" :value="option">{{ option }}</option>
    </select>
    <textarea v-else-if="field.component === 'textarea' || field.component === 'rich-text-editor'" :disabled="disabled || field.read_only" :value="String(modelValue ?? '')" @input="$emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)" />
    <input v-else :type="field.component === 'checkbox' ? 'checkbox' : field.component === 'email-input' ? 'email' : field.component === 'date-picker' ? 'date' : field.component === 'datetime-picker' ? 'datetime-local' : field.component === 'url-input' ? 'url' : ['integer-input', 'decimal-input', 'currency-input'].includes(field.component) ? 'number' : 'text'" :disabled="disabled || field.read_only" :readonly="field.read_only" :checked="field.component === 'checkbox' && Boolean(modelValue)" :value="field.component === 'checkbox' ? undefined : String(modelValue ?? '')" @input="$emit('update:modelValue', ($event.target as HTMLInputElement).type === 'checkbox' ? ($event.target as HTMLInputElement).checked : ($event.target as HTMLInputElement).value)" />
    <small v-if="field.description">{{ field.description }}</small>
    <small v-if="error" role="alert">{{ error }}</small>
  </label>
</template>
