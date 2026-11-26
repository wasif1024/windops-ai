// The investigation screens and evidence viewer in a real browser, through the gateway (M11.1,
// M11.2). The frontend check makes a Reliability Engineer for each screen size. The engineer
// investigates the demo turbine from the screen, follows the run live until it waits for review,
// challenges one of its findings, which is kept beside the report, then opens the evidence, its
// hashes checked again. The run is rehearsed, at no cost. Nothing may scroll sideways (UX-001).
import { expect, test } from "@playwright/test";
import { noSidewaysScroll, openScreen, signIn } from "./helpers";

const tenant = process.env.TENANT ?? "north-sea";
const password = process.env.PLANNER_PASSWORD ?? "";
const turbine = process.env.PLANNER_TURBINE ?? "";

function setting(name: string): string {
  return process.env[`${name}_${test.info().project.name.toUpperCase()}`] ?? "";
}

test("an engineer investigates a turbine, follows it live, and challenges a finding", async ({ page, isMobile }) => {
  test.setTimeout(240_000);
  const [email, title] = [setting("ENGINEER_EMAIL"), setting("INVESTIGATION_TITLE")];
  expect(email && password && title && turbine, "make frontend-check passes the engineer and title").toBeTruthy();
  await page.goto(`/?tenant=${tenant}`);
  await signIn(page, email, password);

  await openScreen(page, isMobile, "Investigations");
  await expect(page.getByRole("heading", { name: "Investigations", level: 2 })).toBeVisible();
  await page.getByRole("combobox", { name: "Turbine" }).selectOption(turbine);
  await page.getByRole("textbox", { name: "What to look into" }).fill(title);
  await page.getByRole("combobox", { name: "Severity" }).selectOption("high");
  await noSidewaysScroll(page);
  await page.getByRole("button", { name: "Investigate" }).click();

  // The case's page at once, then its investigation live (API-003).
  await expect(page.getByRole("heading", { name: title, level: 2 })).toBeVisible();
  await expect(page.getByRole("list", { name: "Progress" })).toContainText("Step", { timeout: 120_000 });
  await noSidewaysScroll(page);
  await page.screenshot({ path: test.info().outputPath("investigation-live.png"), fullPage: true });

  // Step 13: the case waits for review, and its findings appear.
  const findings = page.getByTestId("findings");
  await expect(findings).toBeVisible({ timeout: 180_000 });
  await expect(page.getByText("Pending review").first()).toBeVisible();
  const finding = findings.locator('[data-testid^="finding-"]').first();
  await finding.getByRole("button", { name: "Review" }).click();
  await finding.getByRole("textbox").fill("The site replaced this part last month.");
  await finding.getByRole("button", { name: "Challenge" }).click();
  await expect(finding.getByRole("list", { name: "Reviews" })).toContainText("Challenge", { timeout: 30_000 });
  await expect(finding.getByRole("list", { name: "Reviews" })).toContainText("The site replaced this part last month.");
  await noSidewaysScroll(page);
  await page.screenshot({ path: test.info().outputPath("investigation-findings.png"), fullPage: true });

  // The evidence it rests on, as frozen, its hashes checked again (M11.2).
  await page.getByRole("link", { name: "Open the evidence" }).click();
  await expect(page.getByTestId("snapshot")).toContainText("Hashes match", { timeout: 30_000 });
  const telemetry = page.getByTestId("evidence-telemetry_snapshot");
  await expect(telemetry.getByTestId("data-quality")).toContainText("readings checked as good");
  await expect(page.getByRole("list", { name: "Turbine" })).toContainText(turbine);
  await noSidewaysScroll(page);
  await page.screenshot({ path: test.info().outputPath("evidence.png"), fullPage: true });
  await page.getByRole("link", { name: "← The case" }).click();

  // Back on the list, the case is there, waiting for review.
  await page.getByRole("link", { name: "← Investigations" }).click();
  await expect(page.getByRole("list", { name: "Cases" })).toContainText(title);
});
