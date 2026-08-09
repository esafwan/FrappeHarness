import { expect, test } from "@playwright/test";

const username = process.env.FRAPPE_HARNESS_LIVE_TASK_USER;
const password = process.env.FRAPPE_HARNESS_LIVE_TASK_PASSWORD;
const recordPath = process.env.FRAPPE_HARNESS_LIVE_TASK_RECORD_PATH;

if (!username || !password || !recordPath) {
  throw new Error(
    "FRAPPE_HARNESS_LIVE_TASK_USER, FRAPPE_HARNESS_LIVE_TASK_PASSWORD, and " +
      "FRAPPE_HARNESS_LIVE_TASK_RECORD_PATH are required; credentials are process-local only",
  );
}
if (process.env.FRAPPE_HARNESS_LIVE_ALLOW_MUTATION !== "0") {
  throw new Error("Live Task User acceptance is read-only; set FRAPPE_HARNESS_LIVE_ALLOW_MUTATION=0");
}
if (!recordPath.startsWith("/") || recordPath.startsWith("//") || recordPath.includes("\\")) {
  throw new Error("FRAPPE_HARNESS_LIVE_TASK_RECORD_PATH must be a relative same-origin path");
}
const normalizedRecordPath = new URL(recordPath, "http://live-path.invalid");
if (normalizedRecordPath.origin !== "http://live-path.invalid") {
  throw new Error("FRAPPE_HARNESS_LIVE_TASK_RECORD_PATH must not contain an external origin");
}

test("Task User cannot see a delete action on an existing Task", async ({ page }) => {
  const appPage = "/task_tracker.html";
  await page.goto(`/login?redirect-to=${encodeURIComponent(appPage)}`);
  await page.locator("#login_email").fill(username);
  await page.locator("#login_password").fill(password);
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.waitForURL((url) => !url.pathname.endsWith("/login"));
  await page.goto(appPage);
  await page.evaluate((path) => {
    history.replaceState({}, "", path);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, recordPath);
  await expect(page.getByRole("heading", { name: "Task" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Delete" })).toHaveCount(0);
});
