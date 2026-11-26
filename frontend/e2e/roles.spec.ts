// The roles no other journey signs in as (M11.8), so every SRS 3.2 role is seen in a real browser,
// through the gateway, with exactly its screens. make frontend-check makes an Auditor and an
// Operations Manager for each screen size, signing in with a password alone. Nothing may scroll
// sideways (UX-001).
import { expect, test } from "@playwright/test";
import { covers, expectMenu } from "./coverage";
import { noSidewaysScroll, openScreen, signIn } from "./helpers";

const tenant = process.env.TENANT ?? "north-sea";
const password = process.env.PLANNER_PASSWORD ?? "";

function setting(name: string): string {
  return process.env[`${name}_${test.info().project.name.toUpperCase()}`] ?? "";
}

test("an auditor reads the trail and the library, and changes nothing", covers("auditor", "audit-explorer", "knowledge-library"), async ({ page, isMobile }) => {
  const email = setting("AUDITOR_EMAIL");
  expect(email && password, "make frontend-check passes the auditor").toBeTruthy();
  await page.goto(`/?tenant=${tenant}`);
  await signIn(page, email, password);
  await expectMenu(page, isMobile, "auditor");

  await openScreen(page, isMobile, "Audit explorer");
  await expect(page.getByTestId("audit-records")).toBeVisible({ timeout: 30_000 });
  await page.getByRole("button", { name: "Check the chain" }).click();
  await expect(page.getByTestId("chain-check")).toContainText("Intact", { timeout: 60_000 });
  await noSidewaysScroll(page);

  // Reading the library, but no part of managing it.
  await openScreen(page, isMobile, "Knowledge library");
  await expect(page.getByTestId("documents")).toContainText("WS-PRC-GBX-012", { timeout: 30_000 });
  await expect(page.getByRole("form", { name: "Upload a document" })).toHaveCount(0);
  await noSidewaysScroll(page);
});

test("an operations manager follows the fleet and its cases", covers("operations-manager", "fleet-overview", "investigation-list"), async ({ page, isMobile }) => {
  const email = setting("OPERATIONS_MANAGER_EMAIL");
  expect(email && password, "make frontend-check passes the operations manager").toBeTruthy();
  await page.goto(`/?tenant=${tenant}`);
  await signIn(page, email, password);
  await expectMenu(page, isMobile, "operations-manager");

  await openScreen(page, isMobile, "Fleet overview");
  await expect(page.locator('[data-testid^="turbine-"]').first()).toBeVisible({ timeout: 30_000 });
  await noSidewaysScroll(page);
  await openScreen(page, isMobile, "Investigations");
  await expect(page.getByRole("list", { name: "Cases" })).toBeVisible({ timeout: 30_000 });
  await noSidewaysScroll(page);
});
