// The alarm console in a real browser, through the gateway (M5.6). The frontend check has a demo
// turbine's device raise a check-only alarm for each screen size; the operator finds it, opens
// it, acknowledges it with a comment, and sees who did it in its history. Nothing may scroll
// sideways (UX-001).
import { expect, test } from "@playwright/test";
import { noSidewaysScroll, openScreen, signIn } from "./helpers";

const email = process.env.OPERATOR_EMAIL ?? "";
const password = process.env.OPERATOR_PASSWORD ?? "";
const tenant = process.env.TENANT ?? "north-sea";

test("an operator finds a new alarm, acknowledges it, and sees it in its history", async ({ page, isMobile }) => {
  const tag = process.env[`ALARM_TAG_${test.info().project.name.toUpperCase()}`] ?? "";
  expect(email && password && tag, "make frontend-check passes the operator and the alarm").toBeTruthy();
  await page.goto(`/?tenant=${tenant}`);
  await signIn(page, email, password);

  await openScreen(page, isMobile, "Alarm console");
  const list = page.getByRole("list", { name: "Alarms" });
  // The Alarm Service makes the device's report an alarm within seconds; the console refreshes.
  const row = list.getByRole("link").filter({ hasText: tag });
  await expect(row).toBeVisible({ timeout: 45_000 });
  await expect(row).toContainText("Active");
  await noSidewaysScroll(page);
  await page.screenshot({ path: test.info().outputPath("alarms.png"), fullPage: true });

  await row.click();
  await expect(page.getByRole("link", { name: "← Alarm console" })).toBeVisible();
  // The device sent no severity, so it comes from the versioned catalogue (FR-ALM-004).
  await expect(page.getByText(/From the alarm catalogue/)).toBeVisible();
  await page.getByLabel("Comment, if any").fill("Checked from the console");
  await page.getByRole("button", { name: "Acknowledge" }).click();
  const history = page.getByRole("list", { name: "History" });
  await expect(history).toContainText("Active → Acknowledged", { timeout: 20_000 });
  await expect(history).toContainText("Checked from the console");
  await expect(page.getByRole("button", { name: "Acknowledge" })).toHaveCount(0);
  await expect(page.getByLabel("Reason to shelve")).toBeVisible();
  await noSidewaysScroll(page);
  await page.screenshot({ path: test.info().outputPath("alarm.png"), fullPage: true });

  await page.getByRole("link", { name: "← Alarm console" }).click();
  await page.getByRole("group", { name: "Show" }).getByRole("button", { name: "Acknowledged" }).click();
  await expect(list.getByRole("link").filter({ hasText: tag })).toContainText("Acknowledged");
});
