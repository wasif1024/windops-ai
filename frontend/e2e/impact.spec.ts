// The production impact view and the weather and source freshness panel in a real browser,
// through the gateway (M11.3). The demo operator reads the forecast for the demo turbine's site
// and how fresh each source is, then estimates an action for an active case the frontend check
// opened, and reads every figure with its range and assumptions. Nothing may scroll sideways
// (UX-001).
import { expect, test } from "@playwright/test";
import { noSidewaysScroll, openScreen, signIn } from "./helpers";
import { covers, expectMenu } from "./coverage";

const tenant = process.env.TENANT ?? "north-sea";
const turbine = process.env.PLANNER_TURBINE ?? "";

function setting(name: string): string {
  return process.env[`${name}_${test.info().project.name.toUpperCase()}`] ?? "";
}

test("an operator reads the forecast and its freshness, then estimates a case's impact", covers("control-room-operator", "production-impact", "weather-freshness"), async ({ page, isMobile }) => {
  test.setTimeout(120_000);
  const [email, password, caseTitle] = [process.env.OPERATOR_EMAIL ?? "", process.env.OPERATOR_PASSWORD ?? "", setting("PLANNER_CASE")];
  expect(email && password && turbine && caseTitle, "make frontend-check passes the operator, turbine and case").toBeTruthy();
  await page.goto(`/?tenant=${tenant}`);
  await signIn(page, email, password);
  await expectMenu(page, isMobile, "control-room-operator");

  await openScreen(page, isMobile, "Weather and sources");
  await page.getByRole("combobox", { name: "Turbine" }).selectOption(turbine);
  const freshness = page.getByTestId("freshness");
  await expect(freshness).toContainText(/Fresh|Stale|Unavailable/, { timeout: 30_000 });
  await expect(freshness).toContainText("Telemetry");
  await page.getByRole("button", { name: "3 days" }).click();
  await expect(page.getByTestId("forecast-summary")).toContainText("of 72");
  await noSidewaysScroll(page);
  await page.screenshot({ path: test.info().outputPath("weather.png"), fullPage: true });

  await openScreen(page, isMobile, "Production impact");
  const cases = page.getByRole("combobox", { name: "Active case" });
  const option = cases.locator("option", { hasText: caseTitle });
  await cases.selectOption({ value: (await option.getAttribute("value")) ?? "" });
  await page.getByRole("combobox", { name: "Action" }).selectOption("inspection");
  await page.getByRole("spinbutton", { name: "Downtime, hours" }).fill("4");
  await page.getByRole("button", { name: "Estimate" }).click();
  const result = page.getByTestId("impact-result");
  await expect(result).toContainText("Hash matches", { timeout: 30_000 });
  await expect(result.getByRole("list", { name: "Figures" })).toContainText("Range");
  await expect(result.getByRole("list", { name: "Assumptions" }).locator("li").first()).toBeVisible();
  await noSidewaysScroll(page);
  await page.screenshot({ path: test.info().outputPath("impact.png"), fullPage: true });
});
