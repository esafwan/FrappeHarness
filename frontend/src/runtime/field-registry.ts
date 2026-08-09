import type { Component } from "vue";
import FieldControl from "../components/FieldControl.vue";

export const FIELD_COMPONENTS: Readonly<Record<string, Component>> = Object.freeze({
  "text-input": FieldControl, "email-input": FieldControl, "telephone-input": FieldControl,
  "url-input": FieldControl, textarea: FieldControl, "rich-text-editor": FieldControl,
  "integer-input": FieldControl, "decimal-input": FieldControl, "currency-input": FieldControl,
  checkbox: FieldControl, "date-picker": FieldControl, "datetime-picker": FieldControl,
  select: FieldControl, "link-autocomplete": FieldControl, "form-section": FieldControl,
  "column-break": FieldControl,
});

export const COMPONENT_NAMES = Object.freeze(Object.keys(FIELD_COMPONENTS));

export function fieldComponent(name: string): Component {
  const component = FIELD_COMPONENTS[name];
  if (!component) throw new Error(`Unsupported frontend field component: ${name}`);
  return component;
}
