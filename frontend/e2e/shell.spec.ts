// A real sign-in, in a real browser, through the gateway and Keycloak's own pages.
import { expect, test } from "@playwright/test";

const email = process.env.OPERATOR_EMAIL ?? "";
const password = process.env.OPERATOR_PASSWORD ?? "";
const tenant = process.env.TENANT ?? "north-sea";

test.beforeAll(() => {
  expect(email && password, "make frontend-check passes the demo operator's login").toBeTruthy();
});

test("a control room operator signs in to a branded, role-aware shell", async ({ page, request }) => {
  const document = await page.goto(`/?tenant=${tenant}`);
  const headers = document?.headers() ?? {};
  expect(headers["content-security-policy"]).toContain("default-src 'self'");
  expect(headers["content-security-policy"]).toContain("frame-ancestors 'none'");
  expect(headers["x-content-type-options"]).toBe("nosniff");

  const publicBranding = await (await request.get(`/api/v1/public/branding/${tenant}`)).json();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(
    publicBranding.login_title || "Sign in to WindOps",
  );

  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(/\/auth\/realms\/windops\//);
  await page.locator("#username").fill(email);
  await page.locator("#password").fill(password);
  await page.locator("#kc-login").click();

  await expect(page.getByTestId("user-email")).toHaveText(email);
  await expect(page.getByTestId("tenant-name")).toHaveText(publicBranding.display_name);
  const navigation = page.getByRole("navigation", { name: "Screens" });
  await expect(navigation.getByRole("link", { name: "Alarm console" })).toBeVisible();
  await expect(navigation.getByRole("link", { name: "Administration" })).toHaveCount(0);
  await expect(navigation.getByRole("link", { name: "Approval queue" })).toHaveCount(0);

  await navigation.getByRole("link", { name: "Alarm console" }).click();
  await expect(page.getByText("This screen arrives in Phase 5.")).toBeVisible();

  // Typing an address does not get round the menu: the screen says it is not available.
  await page.evaluate(() => window.history.pushState(null, "", "/screens/admin"));
  await page.evaluate(() => window.dispatchEvent(new PopStateEvent("popstate")));
  await expect(page.getByRole("heading", { name: "Not available" })).toBeVisible();

  // Tokens stay in memory, where script running later cannot read them back from storage.
  const stored = await page.evaluate(() =>
    [...Object.values(window.localStorage), ...Object.values(window.sessionStorage)].join(" "),
  );
  expect(stored).not.toMatch(/eyJ[\w-]+\.eyJ/);

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
});
