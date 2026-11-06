// What the browser journeys share: signing in through Keycloak's own pages, authenticator codes,
// and the phone-size checks (UX-001).
import { createHmac } from "node:crypto";
import { expect, type Page } from "@playwright/test";

const PERIOD = 30;
const enrolledAt = Number(process.env.ENROLLED_AT ?? "0");

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
export async function freshCode(key: string): Promise<string> {
  const used = Math.floor(enrolledAt / PERIOD);
  while (Math.floor(Date.now() / 1000 / PERIOD) <= used) await new Promise((done) => setTimeout(done, 1000));
  return code(key, Date.now() / 1000);
}

/**
 * Fill a Keycloak form and submit it. The gateway allows 5 sign-ins a minute from one address
 * (SRS 7.5), and every journey here comes from the same one, so a refusal waits out the minute
 * and sends the form again, filled afresh, as a person would.
 */
export async function submitToKeycloak(page: Page, fill: () => Promise<void>): Promise<void> {
  for (let attempt = 1; ; attempt += 1) {
    await fill();
    const answered = page.waitForResponse(
      (response) => response.request().method() === "POST" && response.url().includes("/login-actions/"),
    );
    await page.locator("#kc-login").click();
    if ((await answered).status() !== 429 || attempt === 2) return;
    await page.waitForTimeout(61_000);
    await page.goBack();
  }
}

/** Sign in with a password from the app's own sign-in page. */
export async function signIn(page: Page, email: string, password: string): Promise<void> {
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(/\/auth\/realms\/[^/]+\//);
  await submitToKeycloak(page, async () => {
    await page.locator("#username").fill(email);
    await page.locator("#password").fill(password);
  });
  await expect(page.getByTestId("tenant-name")).toBeVisible();
}

/** Answer the step-up prompt with a fresh authenticator code, and come back to the console. */
export async function stepUp(page: Page, key: string): Promise<void> {
  await page.getByRole("button", { name: "Verify now" }).click();
  await page.waitForURL(/\/auth\/realms\/[^/]+\//);
  // The session already has the password, so Keycloak asks only for a code (SEC-003).
  await expect(page.locator("#password")).toHaveCount(0);
  await submitToKeycloak(page, async () => {
    await page.locator("#otp").fill(await freshCode(key));
  });
}

export async function noSidewaysScroll(page: Page): Promise<void> {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow, "the page must not scroll sideways").toBeLessThanOrEqual(0);
}

/** Choose a screen from the sidebar, or from the menu on a phone. */
export async function openScreen(page: Page, isMobile: boolean, name: string): Promise<void> {
  if (isMobile) await page.getByRole("button", { name: "Open the menu" }).click();
  const menu = isMobile
    ? page.getByRole("dialog", { name: "Screens" })
    : page.getByRole("navigation", { name: "Screens" });
  await menu.getByRole("link", { name }).click();
  await expect(page.getByRole("heading", { name, level: 2 })).toBeVisible();
}
