// Capture the console for the README, signed in as a Maintenance Planner, against the live demo
// fleet and the data `make readme-screens` seeds for the picture. It asserts only what it needs to
// wait for: these are pictures, and `make frontend-check` is the test.
import { expect, type Page, test } from "@playwright/test";
import { openScreen, signIn } from "../e2e/helpers";

const out = process.env.SCREENS_DIR ?? "screens-out";
const tenant = process.env.TENANT ?? "north-sea";
const email = process.env.PLANNER_EMAIL ?? "";
const password = process.env.PLANNER_PASSWORD ?? "";
const turbine = process.env.SCREEN_TURBINE ?? "dogger-east-t01";

async function shot(page: Page, name: string): Promise<void> {
  // Let live values and charts settle, and the cursor leave the last thing it clicked.
  await page.mouse.move(0, 0);
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${out}/${name}.png` });
}

async function start(page: Page): Promise<void> {
  expect(email && password, "make readme-screens passes the planner").toBeTruthy();
  await page.goto(`/?tenant=${tenant}`);
  await signIn(page, email, password);
}

test("the console on a desktop", async ({ page, isMobile }) => {
  test.skip(isMobile, "the desktop screens");
  await start(page);

  await openScreen(page, false, "Fleet overview");
  await expect(page.getByTestId(`turbine-${turbine}`)).toBeVisible({ timeout: 30_000 });
  await shot(page, "fleet-overview");

  await page.getByTestId(`turbine-${turbine}`).click();
  // A few minutes of live readings, so the charts have lines.
  await page.waitForTimeout(8000);
  await shot(page, "turbine-detail");

  await openScreen(page, false, "Alarm console");
  await expect(page.getByRole("list", { name: "Alarms" }).getByRole("link")).toHaveCount(3, { timeout: 45_000 });
  await shot(page, "alarm-console");

  await openScreen(page, false, "Maintenance planner");
  const agents = page.getByRole("list", { name: "Work orders" }).getByRole("link").filter({ hasText: "Agent's draft" });
  await expect(agents).toHaveCount(1, { timeout: 20_000 });
  await shot(page, "maintenance-planner");

  await agents.click();
  await expect(page.getByTestId("validation")).toBeVisible();
  await shot(page, "work-order");

  await openScreen(page, false, "Parts lookup");
  await page.getByLabel("Fits turbine").fill(turbine);
  await page.getByRole("button", { name: "Search" }).click();
  await expect(page.getByTestId("parts-context")).toBeVisible({ timeout: 20_000 });
  await shot(page, "parts-lookup");
});

test("the console on a phone", async ({ page, isMobile }) => {
  test.skip(!isMobile, "the phone screen");
  await start(page);
  await openScreen(page, true, "Maintenance planner");
  await page.getByRole("list", { name: "Work orders" }).getByRole("link").filter({ hasText: "Agent's draft" }).click();
  await expect(page.getByTestId("validation")).toBeVisible();
  await shot(page, "phone-work-order");
});
