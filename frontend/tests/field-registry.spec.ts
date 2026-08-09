import { describe, expect, it } from "vitest";
import { COMPONENT_NAMES, fieldComponent } from "../src/runtime/field-registry";
describe("field registry", () => {
  it("covers the fixed V1 component vocabulary", () => expect([...COMPONENT_NAMES].sort()).toEqual(["checkbox", "column-break", "currency-input", "date-picker", "datetime-picker", "decimal-input", "email-input", "form-section", "integer-input", "link-autocomplete", "rich-text-editor", "select", "telephone-input", "text-input", "textarea", "url-input"]));
  it("rejects undeclared components", () => expect(() => fieldComponent("script-executor")).toThrow("Unsupported"));
});
