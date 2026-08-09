import { expect, test } from "@playwright/test";
import { installFakeFrappe, type FakeApi } from "./fixture";

const api = (): FakeApi => ({ requests: [] });

test("user browser flow is manifest-bounded: list/filter/link/edit and no delete", async ({ page }) => {
  const fake = api(); await installFakeFrappe(page, "user", fake); await page.goto("/task_tracker/task");
  await expect(page.getByRole("navigation")).toContainText("Projects"); await expect(page.getByRole("heading", { name: "Tasks" })).toBeVisible(); await expect(page.getByText("Golden Task")).toBeVisible();
  await page.locator("form").first().getByRole("textbox").first().fill("Golden"); await page.getByRole("button", { name: "Apply" }).click();
  await expect.poll(() => fake.requests.filter((entry) => entry.url.pathname === "/api/resource/Task").at(-1)?.url.searchParams.get("filters")).toBe('[["title","contains","Golden"]]');
  const listRequest = fake.requests.find((entry) => entry.url.pathname === "/api/resource/Task"); expect(listRequest?.url.searchParams.get("fields")).toBe('["title","project","status","name"]');
  await page.getByRole("button", { name: "Golden Task" }).click(); await expect(page.getByRole("heading", { name: "Task" })).toBeVisible();
  const projectChoice = page.getByRole("button", { name: "Golden Project (PROJ-1)" }); await expect(projectChoice).toBeVisible(); await projectChoice.click(); await page.getByRole("button", { name: "Save" }).click();
  await expect.poll(() => fake.requests.some((entry) => entry.method === "PUT" && JSON.stringify(entry.body).includes("PROJ-1"))).toBeTruthy(); expect(fake.requests.find((entry) => entry.method === "PUT")?.csrf).toBe("e2e-csrf-token"); await expect(page.getByRole("button", { name: "Delete" })).toHaveCount(0);
});

test("create validates locally, preserves loading/empty states, and sends only declared fields", async ({ page }) => {
  const fake = api(); fake.empty = true; fake.delayList = true; await installFakeFrappe(page, "user", fake); await page.goto("/task_tracker/task");
  await expect(page.getByText("Loading…")).toBeVisible(); await expect(page.getByText("No Task records.")).toBeVisible(); await page.getByRole("button", { name: "New Task" }).click(); await page.getByRole("button", { name: "Save" }).click(); await expect(page.getByText("Subject is required")).toBeVisible();
  await page.getByLabel("Subject").fill("New Task"); await page.getByRole("button", { name: "Golden Project (PROJ-1)" }).click(); await page.getByRole("button", { name: "Save" }).click();
  await expect.poll(() => fake.requests.find((entry) => entry.method === "POST")?.body).toEqual({ title: "New Task", project: "PROJ-1", status: "Open" }); expect(fake.requests.find((entry) => entry.method === "POST")?.csrf).toBe("e2e-csrf-token");
});

test("manager deletion requires confirmation and normalizes a denial", async ({ page }) => {
  const fake = api(); fake.deleteMode = "denied"; await installFakeFrappe(page, "manager", fake); await page.goto("/task_tracker/task/TASK-1"); await expect(page.getByRole("button", { name: "Delete" })).toBeVisible(); await page.getByRole("button", { name: "Delete" }).click(); await expect(page.getByRole("alertdialog")).toBeVisible(); await page.getByRole("button", { name: "Confirm delete" }).click(); await expect(page.getByRole("alert")).toContainText("Permission denied"); expect(fake.requests.some((entry) => entry.method === "DELETE")).toBeTruthy(); expect(fake.requests.find((entry) => entry.method === "DELETE")?.csrf).toBe("e2e-csrf-token");
});

test("manager deletion succeeds only after confirmation", async ({ page }) => {
  const fake = api(); await installFakeFrappe(page, "manager", fake); await page.goto("/task_tracker/task/TASK-1"); await page.getByRole("button", { name: "Delete" }).click(); await page.getByRole("button", { name: "Confirm delete" }).click(); await expect(page).toHaveURL(/\/task_tracker\/task$/); expect(fake.requests.some((entry) => entry.method === "DELETE")).toBeTruthy(); expect(fake.requests.find((entry) => entry.method === "DELETE")?.csrf).toBe("e2e-csrf-token");
});
