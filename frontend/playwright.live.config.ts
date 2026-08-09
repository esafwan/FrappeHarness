import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.FRAPPE_HARNESS_LIVE_BASE_URL;
if (!baseURL) {
  throw new Error("FRAPPE_HARNESS_LIVE_BASE_URL is required for the opt-in live acceptance");
}
const parsedBaseURL = new URL(baseURL);
if (!/^https?:$/.test(parsedBaseURL.protocol) || parsedBaseURL.username || parsedBaseURL.password) {
  throw new Error("FRAPPE_HARNESS_LIVE_BASE_URL must be an http(s) URL without embedded credentials");
}

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/live-task-user.spec.ts",
  fullyParallel: false,
  use: { baseURL, ...devices["Desktop Chrome"], trace: "off", screenshot: "off", video: "off" },
});
