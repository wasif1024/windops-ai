// A Tenant Administrator uses the console in a real browser, through the gateway and Keycloak,
// at desktop size and at phone size (SRS 9.4, UX-001). make frontend-check sets up each one's
// authenticator first, and deletes every user afterwards.
import { expect, test } from "@playwright/test";
import { noSidewaysScroll, openScreen, signIn, stepUp } from "./helpers";

const password = process.env.ADMIN_PASSWORD ?? "";
const operator = process.env.OPERATOR_EMAIL ?? "";
const tenant = process.env.TENANT ?? "north-sea";

function account(project: string) {
  const upper = project.toUpperCase();
  return {
    email: process.env[`ADMIN_EMAIL_${upper}`] ?? "",
    key: process.env[`ADMIN_TOTP_KEY_${upper}`] ?? "",
    added: process.env[`ADDED_EMAIL_${upper}`] ?? "",
  };
}

test.describe.configure({ mode: "serial" });

test("a Tenant Administrator manages users, roles and permissions, with step-up", async ({ page, isMobile }) => {
  const me = account(test.info().project.name);
  expect(me.email && me.key && password, "make frontend-check passes each administrator").toBeTruthy();

  await page.goto(`/?tenant=${tenant}`);
  await signIn(page, me.email, password);

  await openScreen(page, isMobile, "Administration");
  const tabs = page.getByRole("navigation", { name: "Administration" });
  await expect(tabs.getByRole("link")).toHaveText(["Users", "Role permissions", "Access denials", "Branding"]);
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
  const prompt = page.getByRole("alert").filter({ hasText: "needs your second factor" });
  await expect(prompt).toBeVisible();
  await noSidewaysScroll(page);
  if (isMobile) {
    // On a phone, the prompt can be put off. The desktop journey goes on to verify.
    await prompt.getByRole("button", { name: "Not now" }).click();
    await expect(prompt).toHaveCount(0);
    await tabs.getByRole("link", { name: "Branding" }).click();
    await expect(page.getByRole("region", { name: "Look and sign-in page" })).toBeVisible();
    await noSidewaysScroll(page);
    return;
  }
  await stepUp(page, me.key);
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

  // The tenant's own branding (SRS 9.4). Saving it unchanged, and a domain set then removed,
  // leave the demo tenant as it was.
  await tabs.getByRole("link", { name: "Branding" }).click();
  const look = page.getByRole("region", { name: "Look and sign-in page" });
  await look.getByRole("button", { name: "Save branding" }).click();
  await expect(look.getByRole("status")).toHaveText(/Saved/);
  const emails = page.getByRole("region", { name: "Emails" });
  await expect(emails.getByRole("button", { name: "Save this email" })).toHaveCount(3);
  const domain = page.getByRole("region", { name: "Custom domain" });
  // One left by a run that stopped part way is removed first.
  if (await domain.getByRole("button", { name: "Remove domain" }).isVisible()) {
    await domain.getByRole("button", { name: "Remove domain" }).click();
  }
  await domain.getByLabel(/Domain, such as/).fill(me.added.replace(/^[^@]+@/, "ops."));
  await domain.getByRole("button", { name: "Set domain" }).click();
  await expect(page.getByTestId("domain-token")).not.toBeEmpty();
  await domain.getByRole("button", { name: "Remove domain" }).click();
  await expect(domain.getByRole("button", { name: "Set domain" })).toBeVisible();
  await noSidewaysScroll(page);
  await page.screenshot({ path: test.info().outputPath("branding.png"), fullPage: true });
});
