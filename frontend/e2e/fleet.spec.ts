// The fleet screens against the live simulator, in a real browser, through the gateway (M4.8). It
// runs once at desktop size and once at phone size, and nothing may scroll sideways (UX-001).
import { expect, type Page, test } from "@playwright/test";
import { signIn } from "./helpers";

const email = process.env.OPERATOR_EMAIL ?? "";
const password = process.env.OPERATOR_PASSWORD ?? "";
const tenant = process.env.TENANT ?? "north-sea";

test.beforeAll(() => {
  expect(email && password, "make frontend-check passes the demo operator's login").toBeTruthy();
});

async function noSidewaysScroll(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow, "the page must not scroll sideways").toBeLessThanOrEqual(0);
}

async function openScreen(page: Page, name: string, isMobile: boolean) {
  if (isMobile) await page.getByRole("button", { name: "Open the menu" }).click();
  const menu = isMobile ? page.getByRole("dialog", { name: "Screens" }) : page.getByRole("navigation", { name: "Screens" });
  await menu.getByRole("link", { name }).click();
}

test("an operator watches the fleet, a turbine's readings and its live charts", async ({ page, isMobile }) => {
  await page.goto(`/?tenant=${tenant}`);
  await signIn(page, email, password);

  await openScreen(page, "Fleet overview", isMobile);
  await expect(page.getByRole("heading", { name: "Fleet overview" })).toBeVisible();
  // The simulator streams every demo turbine, so the first is live, with power and wind.
  const card = page.locator('[data-testid^="turbine-"]').first();
  await expect(card).toBeVisible();
  await expect(card.locator("[data-freshness]")).toHaveAttribute("data-freshness", "live", { timeout: 30_000 });
  await expect(page.getByText(/operating · .* now/).first()).toBeVisible();
  await noSidewaysScroll(page);
  await page.screenshot({ path: test.info().outputPath("fleet.png"), fullPage: true });

  await card.click();
  await expect(page.getByRole("link", { name: "← Fleet overview" })).toBeVisible();
  await expect(page.getByRole("list", { name: "Latest readings" })).toBeVisible();
  await expect(page.getByRole("list", { name: "Components" })).toBeVisible();
  // A chart draws a line once its window has readings.
  const power = page.getByTestId("chart-power");
  await expect(power).toBeVisible({ timeout: 30_000 });
  await expect(power.locator("path")).toHaveAttribute("d", /^M[\d.]+,[\d.]+L/);
  await expect(page.getByTestId("latest-power")).not.toHaveText("—");
  await page.getByRole("button", { name: "5 min", exact: true }).click();
  await expect(page.getByRole("button", { name: "5 min", exact: true })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("group", { name: "Metrics" }).getByText("Main bearing temperature", { exact: true }).click();
  await expect(page.getByTestId("chart-main_bearing_temperature")).toBeVisible({ timeout: 30_000 });
  await noSidewaysScroll(page);
  await page.screenshot({ path: test.info().outputPath("turbine.png"), fullPage: true });

  await page.getByRole("link", { name: "← Fleet overview" }).click();
  await expect(page.getByRole("heading", { name: "Fleet overview" })).toBeVisible();

  await openScreen(page, "Telemetry", isMobile);
  const chooser = page.getByLabel("Show telemetry for");
  await expect(chooser).toBeVisible();
  const second = await chooser.locator("option").nth(1).getAttribute("value");
  expect(second).toBeTruthy();
  await chooser.selectOption(second ?? "");
  await expect(page).toHaveURL(new RegExp(`turbine=${second}`));
  await expect(page.getByTestId("chart-power")).toBeVisible({ timeout: 30_000 });
  await noSidewaysScroll(page);
});
