// Capture the console for the README, each screen as the role that uses it, against the live demo
// fleet and the data `make readme-screens` seeds for the picture. It asserts only what it needs to
// wait for: these are pictures, and `make frontend-check` is the test.
//
// The gateway allows 5 sign-ins a minute from one address, and the helpers wait out a refusal.
// The investigation takes a few minutes, which spreads the sign-ins out.
import { expect, type Page, test } from "@playwright/test";
import { openScreen, signIn } from "../e2e/helpers";

const out = process.env.SCREENS_DIR ?? "screens-out";
const tenant = process.env.TENANT ?? "north-sea";
const password = process.env.SCREEN_PASSWORD ?? "";
const turbine = process.env.SCREEN_TURBINE ?? "dogger-east-t01";
const title = process.env.SCREEN_INVESTIGATION ?? "";
const workOrder = process.env.SCREEN_WORK_ORDER ?? "";
const AGENTS_DRAFT = "Inspect the gearbox oil cooling on DE-T01";

async function shot(page: Page, name: string): Promise<void> {
  // Let live values and charts settle, and the cursor leave the last thing it clicked.
  await page.mouse.move(0, 0);
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${out}/${name}.png` });
}

async function start(page: Page, role: string): Promise<void> {
  const email = process.env[`SCREEN_${role}_EMAIL`] ?? "";
  expect(email && password, `make readme-screens passes the ${role.toLowerCase()}`).toBeTruthy();
  await page.goto(`/?tenant=${tenant}`);
  await signIn(page, email, password);
}

test("a planner's screens on a desktop", async ({ page, isMobile }) => {
  test.skip(isMobile, "the desktop screens");
  await start(page, "PLANNER");

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
  const agents = page.getByRole("list", { name: "Work orders" }).getByRole("link").filter({ hasText: AGENTS_DRAFT });
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

test("an engineer investigates, on a desktop", async ({ page, isMobile }) => {
  test.skip(isMobile, "the desktop screens");
  test.setTimeout(420_000);
  expect(title, "make readme-screens names the investigation").toBeTruthy();
  await start(page, "ENGINEER");

  // A rehearsed run: the real agent team on a scripted model, so no call reaches Claude.
  await openScreen(page, false, "Investigations");
  await page.getByRole("combobox", { name: "Turbine" }).selectOption(turbine);
  await page.getByRole("textbox", { name: "What to look into" }).fill(title);
  await page.getByRole("combobox", { name: "Severity" }).selectOption("high");
  await page.getByRole("button", { name: "Investigate" }).click();
  await expect(page.getByRole("heading", { name: title, level: 2 })).toBeVisible();
  const progress = page.getByRole("list", { name: "Progress" });
  await expect(progress.getByRole("listitem").nth(3)).toBeVisible({ timeout: 180_000 });
  await shot(page, "investigation-live");

  await expect(page.getByTestId("findings")).toBeVisible({ timeout: 240_000 });
  await expect(page.getByRole("list", { name: "Timeline" }).getByRole("listitem").first()).toBeVisible();
  await page.getByTestId("findings").scrollIntoViewIfNeeded();
  await shot(page, "investigation");

  await page.getByRole("link", { name: "Open the evidence" }).click();
  await expect(page.getByTestId("snapshot")).toContainText("Hashes match", { timeout: 30_000 });
  await shot(page, "evidence");

  // One OpenAI embedding for the question; nothing else is paid for.
  await openScreen(page, false, "Knowledge library");
  await expect(page.getByTestId("documents")).toContainText("WS-PRC-GBX-012 revision 3", { timeout: 30_000 });
  await page.getByRole("textbox", { name: "Question" }).fill("gearbox oil cooling fault");
  await page.getByRole("button", { name: "Search" }).click();
  await expect(page.getByTestId("search-results")).toContainText("kn:", { timeout: 30_000 });
  await shot(page, "knowledge-library");
});

test("a manager's approval queue, on a desktop", async ({ page, isMobile }) => {
  test.skip(isMobile, "the desktop screens");
  expect(workOrder, "make readme-screens passes the submitted work order").toBeTruthy();
  await start(page, "MANAGER");
  await openScreen(page, false, "Approval queue");
  const row = page.getByRole("link", { name: new RegExp(workOrder) });
  await expect(row).toContainText("Pending", { timeout: 30_000 });
  await shot(page, "approval-queue");

  await row.click();
  await expect(page.getByTestId("validation")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("list", { name: "History" })).toContainText("Submitted");
  await shot(page, "approval");
});

test("an auditor's trail, on a desktop", async ({ page, isMobile }) => {
  test.skip(isMobile, "the desktop screens");
  await start(page, "AUDITOR");
  await openScreen(page, false, "Audit explorer");
  await expect(page.getByTestId("audit-records")).toBeVisible({ timeout: 30_000 });
  await page.getByRole("button", { name: "Check the chain" }).click();
  await expect(page.getByTestId("chain-check")).toContainText("Intact", { timeout: 60_000 });
  await shot(page, "audit-explorer");
});

test("an administrator's console, on a desktop", async ({ page, isMobile }) => {
  test.skip(isMobile, "the desktop screens");
  await start(page, "ADMIN");
  // Role permissions: the users list would show whatever check accounts the cluster holds.
  await openScreen(page, false, "Administration");
  await page.getByRole("navigation", { name: "Administration" }).getByRole("link", { name: "Role permissions" }).click();
  await page.getByLabel("Role", { exact: true }).selectOption("maintenance-manager");
  await expect(page.getByRole("checkbox", { name: "maintenance.approve" })).toBeChecked({ timeout: 30_000 });
  await shot(page, "administration");
});

test("the console on a phone", async ({ page, isMobile }) => {
  test.skip(!isMobile, "the phone screens");
  test.setTimeout(240_000);
  await start(page, "PLANNER");
  await openScreen(page, true, "Maintenance planner");
  await page.getByRole("list", { name: "Work orders" }).getByRole("link").filter({ hasText: AGENTS_DRAFT }).click();
  await expect(page.getByTestId("validation")).toBeVisible();
  await shot(page, "phone-work-order");
});

test("the investigation on a phone", async ({ page, isMobile }) => {
  test.skip(!isMobile, "the phone screens");
  await start(page, "ENGINEER");
  await openScreen(page, true, "Investigations");
  await page.getByRole("list", { name: "Cases" }).getByRole("link").filter({ hasText: title }).click();
  await expect(page.getByTestId("findings")).toBeVisible({ timeout: 60_000 });
  await page.getByTestId("findings").scrollIntoViewIfNeeded();
  await shot(page, "phone-investigation");
});
