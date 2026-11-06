// A Tenant Administrator uses the console in a real browser, through the gateway and Keycloak,
// at desktop size and at phone size (SRS 9.4, UX-001). make frontend-check sets up each one's
// authenticator first, and deletes every user afterwards.
import { createHmac } from "node:crypto";
import { expect, type Page, test } from "@playwright/test";

const password = process.env.ADMIN_PASSWORD ?? "";
const operator = process.env.OPERATOR_EMAIL ?? "";
const tenant = process.env.TENANT ?? "north-sea";
const enrolledAt = Number(process.env.ENROLLED_AT ?? "0");
const PERIOD = 30;

function account(project: string) {
  const upper = project.toUpperCase();
  return {
    email: process.env[`ADMIN_EMAIL_${upper}`] ?? "",
    key: process.env[`ADMIN_TOTP_KEY_${upper}`] ?? "",
    added: process.env[`ADDED_EMAIL_${upper}`] ?? "",
  };
}

function base32(text: string): Buffer {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const letter of text.replace(/[\s=]/g, "").toUpperCase()) {
    bits += alphabet.indexOf(letter).toString(2).padStart(5, "0");
  }
  const bytes = bits.match(/.{8}/g) ?? [];
  return Buffer.from(bytes.map((byte) => parseInt(byte, 2)));
}

/** The code an authenticator app shows now (RFC 6238), with the realm's SHA-256. */
function code(key: string, seconds: number): string {
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(seconds / PERIOD)));
  const digest = createHmac("sha256", base32(key)).update(counter).digest();
  const start = (digest[digest.length - 1] ?? 0) & 15;
  return String((digest.readUInt32BE(start) & 0x7fffffff) % 1_000_000).padStart(6, "0");
}

/** A code from a later window than setup used, since Keycloak accepts each code once. */
async function freshCode(key: string): Promise<string> {
  const used = Math.floor(enrolledAt / PERIOD);
  while (Math.floor(Date.now() / 1000 / PERIOD) <= used) await new Promise((done) => setTimeout(done, 1000));
  return code(key, Date.now() / 1000);
}

async function noSidewaysScroll(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow, "the page must not scroll sideways").toBeLessThanOrEqual(0);
}

async function openAdministration(page: Page, isMobile: boolean) {
  if (isMobile) await page.getByRole("button", { name: "Open the menu" }).click();
  const menu = isMobile ? page.getByRole("dialog", { name: "Screens" }) : page.getByRole("navigation", { name: "Screens" });
  await menu.getByRole("link", { name: "Administration" }).click();
  await expect(page.getByRole("heading", { name: "Administration" })).toBeVisible();
}

test.describe.configure({ mode: "serial" });

test("a Tenant Administrator manages users, roles and permissions, with step-up", async ({ page, isMobile }) => {
  const me = account(test.info().project.name);
  expect(me.email && me.key && password, "make frontend-check passes each administrator").toBeTruthy();

  await page.goto(`/?tenant=${tenant}`);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(/\/auth\/realms\/windops\//);
  await page.locator("#username").fill(me.email);
  await page.locator("#password").fill(password);
  await page.locator("#kc-login").click();
  await expect(page.getByTestId("tenant-name")).toBeVisible();

  await openAdministration(page, isMobile);
  const tabs = page.getByRole("navigation", { name: "Administration" });
  await expect(tabs.getByRole("link")).toHaveText(["Users", "Role permissions", "Access denials"]);
  const users = page.getByRole("list", { name: "Users" });
  await expect(users.getByRole("link", { name: operator })).toBeVisible();
  await noSidewaysScroll(page);
  await page.screenshot({ path: test.info().outputPath("users.png"), fullPage: true });

  // The operator's refused attempt, which make frontend-check made, is in the trail (SRS 9.4).
  await tabs.getByRole("link", { name: "Access denials" }).click();
  const denials = page.getByRole("list", { name: "Access denials" });
  await expect(denials.getByText(operator).first()).toBeVisible();
  await expect(denials.getByText("users.manage").first()).toBeVisible();
  await noSidewaysScroll(page);

  await tabs.getByRole("link", { name: "Role permissions" }).click();
  await page.getByLabel("Role", { exact: true }).selectOption("auditor");
  await expect(page.getByRole("checkbox", { name: "audit.read" })).toBeChecked();
  await expect(page.getByRole("checkbox", { name: "users.manage" })).not.toBeChecked();
  await noSidewaysScroll(page);

  // A password-only sign-in may read everything, but a change asks for a second factor.
  await tabs.getByRole("link", { name: "Users" }).click();
  await page.getByLabel("Email").fill(me.added);
  await page.getByRole("button", { name: "Add user" }).click();
  const stepUp = page.getByRole("alert").filter({ hasText: "needs your second factor" });
  await expect(stepUp).toBeVisible();
  await noSidewaysScroll(page);
  if (isMobile) {
    // On a phone, the prompt can be put off. The desktop journey goes on to verify.
    await stepUp.getByRole("button", { name: "Not now" }).click();
    await expect(stepUp).toHaveCount(0);
    return;
  }
  await stepUp.getByRole("button", { name: "Verify now" }).click();
  await page.waitForURL(/\/auth\/realms\/windops\//);
  // The session already has the password, so Keycloak asks only for a code (SEC-003).
  await expect(page.locator("#password")).toHaveCount(0);
  await page.locator("#otp").fill(await freshCode(me.key));
  await page.locator("#kc-login").click();
  await expect(page.getByRole("heading", { name: "Administration" })).toBeVisible();
  expect(new URL(page.url()).searchParams.get("tab")).toBe("users");

  await page.getByLabel("Email").fill(me.added);
  await page.getByLabel("First name").fill("Browser");
  await page.getByRole("button", { name: "Add user" }).click();
  await expect(page.getByTestId("temporary-password")).not.toBeEmpty();
  await page.getByRole("button", { name: "Done" }).click();
  await expect(page.getByTestId("temporary-password")).toHaveCount(0);

  await users.getByRole("link", { name: me.added }).click();
  await expect(page.getByTestId("user-status")).toHaveText("Active");
  await expect(page.getByTestId("mfa-status")).toContainText("Authenticator app: not set up");

  const roles = page.getByRole("region", { name: "Roles" });
  await page.getByLabel("Role", { exact: true }).selectOption("auditor");
  await page.getByRole("button", { name: "Give role" }).click();
  await expect(roles.getByRole("button", { name: "Revoke Auditor" })).toBeVisible();
  await roles.getByRole("button", { name: "Revoke Auditor" }).click();
  await expect(roles.getByRole("button", { name: "Revoke Auditor" })).toHaveCount(0);
  await expect(roles.getByText("Ended roles (1)")).toBeVisible();

  await page.getByRole("button", { name: "Disable user" }).click();
  await expect(page.getByTestId("user-status")).toHaveText("Disabled");
  await page.getByRole("button", { name: "Reactivate user" }).click();
  await expect(page.getByTestId("user-status")).toHaveText("Active");
  await noSidewaysScroll(page);
  await page.screenshot({ path: test.info().outputPath("user.png"), fullPage: true });
});
