// A real sign-in, in a real browser, through the gateway and Keycloak's own pages. It runs once
// at desktop size and once at phone size (UX-001).
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

/** The screens menu: the sidebar on a desktop, or the slide-over menu on a phone. */
async function screensMenu(page: Page, isMobile: boolean) {
  if (isMobile) {
    const open = page.getByRole("button", { name: "Open the menu" });
    await expect(open).toHaveAttribute("aria-expanded", "false");
    await open.click();
    await expect(open).toHaveAttribute("aria-expanded", "true");
    return page.getByRole("dialog", { name: "Screens" });
  }
  await expect(page.getByRole("button", { name: "Open the menu" })).toBeHidden();
  return page.getByRole("navigation", { name: "Screens" });
}

test("a control room operator signs in to a branded, role-aware shell", async ({ page, request, isMobile }) => {
  const document = await page.goto(`/?tenant=${tenant}`);
  const headers = document?.headers() ?? {};
  expect(headers["content-security-policy"]).toContain("default-src 'self'");
  expect(headers["content-security-policy"]).toContain("frame-ancestors 'none'");
  expect(headers["x-content-type-options"]).toBe("nosniff");

  const publicBranding = await (await request.get(`/api/v1/public/branding/${tenant}`)).json();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(
    publicBranding.login_title || "Sign in to WindOps",
  );
  await page.screenshot({ path: test.info().outputPath("sign-in.png") });
  await noSidewaysScroll(page);

  await signIn(page, email, password);

  await expect(page.getByTestId("tenant-name")).toHaveText(publicBranding.display_name);
  if (!isMobile) await expect(page.getByTestId("user-email")).toHaveText(email);
  await noSidewaysScroll(page);

  let menu = await screensMenu(page, isMobile);
  if (isMobile) await expect(menu.getByTestId("menu-user-email")).toHaveText(email);
  await expect(menu.getByRole("link", { name: "Alarm console" })).toBeVisible();
  await expect(menu.getByRole("link", { name: "Administration" })).toHaveCount(0);
  await expect(menu.getByRole("link", { name: "Approval queue" })).toHaveCount(0);

  await menu.getByRole("link", { name: "Alarm console" }).click();
  await expect(page.getByText("This screen arrives in Phase 5.")).toBeVisible();
  // For people to look at, in test-results, which git ignores.
  await page.screenshot({ path: test.info().outputPath("shell.png") });
  // On a phone, choosing a screen closes the menu, so the screen is not hidden behind it.
  if (isMobile) await expect(page.getByRole("dialog", { name: "Screens" })).toHaveCount(0);

  if (isMobile) {
    menu = await screensMenu(page, isMobile);
    await page.keyboard.press("Escape");
    await expect(menu).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Open the menu" })).toBeFocused();
  }

  // Typing an address does not get round the menu: the screen says it is not available.
  await page.evaluate(() => window.history.pushState(null, "", "/screens/admin"));
  await page.evaluate(() => window.dispatchEvent(new PopStateEvent("popstate")));
  await expect(page.getByRole("heading", { name: "Not available" })).toBeVisible();

  // Tokens stay in memory, where script running later cannot read them back from storage.
  const stored = await page.evaluate(() =>
    [...Object.values(window.localStorage), ...Object.values(window.sessionStorage)].join(" "),
  );
  expect(stored).not.toMatch(/eyJ[\w-]+\.eyJ/);

  if (isMobile) menu = await screensMenu(page, isMobile);
  await (isMobile ? menu : page).getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
});
