// The maintenance planner and parts lookup in a real browser, through the gateway (M9.4). The
// frontend check makes a Maintenance Planner, a check-only part that fits the demo turbine with
// stock in its store, and an active case, for each screen size. The planner finds the part, drafts
// a work order with it for the case, sees it validated against inventory and policy, and submits
// it. Nothing may scroll sideways (UX-001).
import { expect, test } from "@playwright/test";
import { noSidewaysScroll, openScreen, signIn } from "./helpers";
import { covers, expectMenu } from "./coverage";

const tenant = process.env.TENANT ?? "north-sea";
const password = process.env.PLANNER_PASSWORD ?? "";
const turbine = process.env.PLANNER_TURBINE ?? "dogger-east-t01";

function setting(name: string): string {
  return process.env[`${name}_${test.info().project.name.toUpperCase()}`] ?? "";
}

/** A datetime-local value, in the browser's own time, some days from now. */
function daysFromNow(days: number, hour: number): string {
  const when = new Date(Date.now() + days * 86_400_000);
  when.setHours(hour, 0, 0, 0);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}T${pad(hour)}:00`;
}

test("a planner finds a part, drafts a work order with it, and submits it", covers("maintenance-planner", "maintenance-planner", "parts-lookup"), async ({ page, isMobile }) => {
  const [email, part, caseTitle] = [setting("PLANNER_EMAIL"), setting("PLANNER_PART"), setting("PLANNER_CASE")];
  expect(email && password && part && caseTitle, "make frontend-check passes the planner, part and case").toBeTruthy();
  await page.goto(`/?tenant=${tenant}`);
  await signIn(page, email, password);
  await expectMenu(page, isMobile, "maintenance-planner");

  // Parts lookup: the parts that fit the turbine, with its store's stock.
  await openScreen(page, isMobile, "Parts lookup");
  await page.getByLabel("Fits turbine").fill(turbine);
  await page.getByLabel("Words").fill(part);
  await page.getByRole("button", { name: "Search" }).click();
  const row = page.getByTestId(`part-${part}`);
  await expect(row).toContainText("2 available");
  await expect(page.getByTestId("parts-context")).toContainText(turbine);
  await noSidewaysScroll(page);
  await page.screenshot({ path: test.info().outputPath("parts.png"), fullPage: true });
  await row.click();
  await expect(page.getByRole("list", { name: "Stores" })).toContainText("2 available");
  await noSidewaysScroll(page);

  // A new draft, with that part, for the case.
  await openScreen(page, isMobile, "Maintenance planner");
  await page.getByRole("link", { name: "New draft" }).click();
  const choice = page.getByLabel("Case", { exact: true }).locator("option", { hasText: caseTitle });
  await page.getByLabel("Case", { exact: true }).selectOption((await choice.getAttribute("value")) ?? "");
  await page.getByLabel("Title", { exact: true }).fill(`Planner screen check: ${part}`);
  await page.getByLabel("Action").selectOption("monitoring");
  await page.getByLabel("Summary").fill("Watch the gearbox oil temperature for a week, with a spare capacitor ready.");
  await page.getByLabel("Task 1").fill("Review the oil temperature trend remotely");
  await page.getByLabel("Hours", { exact: true }).fill("2");
  await page.getByRole("button", { name: "Add a part" }).click();
  await page.getByLabel("Part number").fill(part);
  await page.getByLabel("Starts").fill(daysFromNow(3, 8));
  await page.getByLabel("Ends").fill(daysFromNow(3, 12));
  await noSidewaysScroll(page);
  await page.getByRole("button", { name: "Save draft" }).click();

  // Validated at once: the part is available, and nothing blocks it.
  await expect(page.getByRole("link", { name: "← Maintenance planner" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("list", { name: "Parts" })).toContainText("Available");
  await expect(page.getByTestId("validation")).toContainText("Nothing blocks it.");
  await noSidewaysScroll(page);
  await page.screenshot({ path: test.info().outputPath("work-order.png"), fullPage: true });

  await page.getByRole("button", { name: "Submit for a decision" }).click();
  const history = page.getByRole("list", { name: "History" });
  await expect(history).toContainText("Submitted", { timeout: 30_000 });
  await expect(page.getByRole("button", { name: "Submit for a decision" })).toHaveCount(0);
  await noSidewaysScroll(page);
});
