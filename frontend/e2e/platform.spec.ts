// The Platform Administrator uses the Platform console in a real browser, through the gateway and
// Keycloak, at desktop size and at phone size (M3.10, RBAC-003, UX-001). make frontend-check sets up
// each one's authenticator first, and deletes them afterwards. It suspends and reactivates the
// tenant platform-check, which make platform-api-check also uses, and leaves it active.
import { expect, test } from "@playwright/test";
import { noSidewaysScroll, openScreen, signIn, stepUp } from "./helpers";

const password = process.env.ADMIN_PASSWORD ?? "";
const CHECK_TENANT = "platform-check";
const REASON = "Browser check of break-glass, incident INC-0000";

function account(project: string) {
  const upper = project.toUpperCase();
  return {
    email: process.env[`PLATFORM_EMAIL_${upper}`] ?? "",
    key: process.env[`PLATFORM_TOTP_KEY_${upper}`] ?? "",
  };
}

test("a Platform Administrator manages tenants and break-glass access, with step-up", async ({ page, isMobile }) => {
  const me = account(test.info().project.name);
  expect(me.email && me.key && password, "make frontend-check passes each Platform Administrator").toBeTruthy();

  await page.goto("/");
  await signIn(page, me.email, password);
  await openScreen(page, isMobile, "Platform");
  const tabs = page.getByRole("navigation", { name: "Platform" });
  const tenants = page.getByRole("list", { name: "Tenants" });
  await expect(tenants.getByRole("link", { name: /north-sea/ })).toBeVisible();
  await expect(tenants.getByRole("link", { name: /atlas-wind/ })).toBeVisible();
  await noSidewaysScroll(page);
  await page.screenshot({ path: test.info().outputPath("tenants.png"), fullPage: true });

  // Another tenant's branding is shown, but only its own administrators change it.
  await tenants.getByRole("link", { name: /north-sea/ }).click();
  await expect(page.getByTestId("tenant-status")).toHaveText("Active");
  await expect(page.getByRole("region", { name: "Branding" })).toContainText("Only the tenant's own administrators");
  await expect(page.getByRole("button", { name: /Save/ })).toHaveCount(0);
  await noSidewaysScroll(page);

  // A change asks for a second factor. Creating a tenant that exists changes nothing.
  await tabs.getByRole("link", { name: "Tenants" }).click();
  const create = async () => {
    await page.getByLabel("Tenant ID").fill(CHECK_TENANT);
    await page.getByLabel("Name").fill("Platform Check");
    await page.getByRole("button", { name: "Create tenant" }).click();
  };
  await create();
  const prompt = page.getByRole("alert").filter({ hasText: "needs your second factor" });
  await expect(prompt).toBeVisible();
  if (isMobile) {
    await prompt.getByRole("button", { name: "Not now" }).click();
    await tabs.getByRole("link", { name: "Break-glass access" }).click();
    await expect(page.getByRole("region", { name: "Start break-glass access" })).toBeVisible();
    await noSidewaysScroll(page);
    return;
  }
  await stepUp(page, me.key);
  await expect(page.getByRole("heading", { name: "Platform", level: 2 })).toBeVisible();
  await create();
  await expect(page.getByRole("status")).toHaveText(/platform-check (was created|already existed)/);

  await tenants.getByRole("link", { name: new RegExp(CHECK_TENANT) }).click();
  await page.getByLabel(/Reason for suspending/).fill("Browser check, suspending for a moment");
  await page.getByRole("button", { name: "Suspend tenant" }).click();
  await expect(page.getByTestId("tenant-status")).toHaveText("Suspended");
  await page.getByRole("button", { name: "Reactivate tenant" }).click();
  await expect(page.getByTestId("tenant-status")).toHaveText("Active");

  // Reasoned, time-limited, read-only access to one tenant, which then ends (RBAC-003).
  await tabs.getByRole("link", { name: "Break-glass access" }).click();
  const active = page.getByRole("list", { name: "Active break-glass access" });
  // One left by a run that stopped part way is ended first.
  const leftOver = active.getByRole("button", { name: "End access to north-sea" });
  await page.getByRole("region", { name: "Your active access" }).waitFor();
  if (await leftOver.isVisible()) await leftOver.click();
  await page.getByLabel("Tenant").selectOption("north-sea");
  await page.getByLabel("Minutes").fill("5");
  await page.getByLabel("Reason").fill(REASON);
  await page.getByRole("button", { name: "Start access" }).click();
  await expect(active.getByText(REASON)).toBeVisible();
  await noSidewaysScroll(page);
  await page.screenshot({ path: test.info().outputPath("break-glass.png"), fullPage: true });
  await active.getByRole("button", { name: "End access to north-sea" }).click();
  await expect(page.getByText("None. Access ends by itself when its time is up.")).toBeVisible();
});
