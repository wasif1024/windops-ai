// A Tenant Administrator uses the console in a real browser, through the gateway and Keycloak,
// at desktop size and at phone size (SRS 9.4, UX-001), starting in the audit explorer (M11.6). make frontend-check sets up each one's
// authenticator first, and deletes every user afterwards.
import { expect, test } from "@playwright/test";
import { noSidewaysScroll, openScreen, signIn, stepUp } from "./helpers";
import { covers, expectMenu } from "./coverage";

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

test("a Tenant Administrator manages users, roles and permissions, with step-up", covers(
  "tenant-administrator",
  "audit-explorer",
  "admin-users",
  "admin-roles",
  "admin-permissions",
  "admin-mfa",
  "admin-sessions",
  "admin-branding",
  "admin-approval-policies",
  "admin-mcp-integrations",
  "admin-gateway-policies",
), async ({ page, isMobile }) => {
  const me = account(test.info().project.name);
  expect(me.email && me.key && password, "make frontend-check passes each administrator").toBeTruthy();

  await page.goto(`/?tenant=${tenant}`);
  await signIn(page, me.email, password);
  await expectMenu(page, isMobile, "tenant-administrator");

  // The audit explorer (M11.6): the operator's refused attempt, which make frontend-check made,
  // then everything that actor did, and the hash chain checked again.
  await openScreen(page, isMobile, "Audit explorer");
  await page.getByLabel("Outcome").selectOption("denied");
  await page.getByRole("button", { name: "Apply" }).click();
  const trail = page.getByTestId("audit-records");
  await expect(trail).toContainText("authorization.denied", { timeout: 30_000 });
  const denied = trail.getByTestId("audit-record").first();
  await denied.locator("summary").click();
  await denied.getByRole("button", { name: "Same actor" }).click();
  await expect(page.getByLabel("Actor")).not.toHaveValue("");
  await expect(page.getByLabel("Outcome")).toHaveValue("");
  await expect(page.getByTestId("audit-records")).toBeVisible();
  await page.getByRole("button", { name: "Check the chain" }).click();
  await expect(page.getByTestId("chain-check")).toContainText("Intact", { timeout: 60_000 });
  await noSidewaysScroll(page);
  await page.screenshot({ path: test.info().outputPath("audit.png"), fullPage: true });
  await page.getByRole("link", { name: "Notification deliveries" }).click();
  await expect(page.getByTestId("deliveries").or(page.getByText("No deliveries match."))).toBeVisible();
  await noSidewaysScroll(page);

  await openScreen(page, isMobile, "Administration");
  const tabs = page.getByRole("navigation", { name: "Administration" });
  await expect(tabs.getByRole("link")).toHaveText([
    "Users",
    "Role permissions",
    "Approval policy",
    "Change review",
    "Access denials",
    "MCP integrations",
    "Gateway policies",
    "Branding",
    "Data egress",
  ]);
  // Who may approve, and changes to it waiting for a second administrator (RBAC-007).
  await tabs.getByRole("link", { name: "Approval policy" }).click();
  await expect(page.getByRole("list", { name: "Roles that approve" })).toContainText("Maintenance Manager", { timeout: 30_000 });
  await expect(page.getByTestId("approvers")).toBeVisible();
  await expect(page.getByRole("region", { name: "Changes waiting for a second administrator" })).toBeVisible();
  await noSidewaysScroll(page);

  // What administrators changed, from the audit trail (SRS 9.4): the check's own grants.
  await tabs.getByRole("link", { name: "Change review" }).click();
  await page.getByRole("group", { name: "Kind of change" }).getByRole("button", { name: "Roles" }).click();
  await expect(page.getByTestId("changes")).toContainText("authorization.role.granted", { timeout: 30_000 });
  await noSidewaysScroll(page);

  // MCP integrations and gateway policies, as built into this release.
  await tabs.getByRole("link", { name: "MCP integrations" }).click();
  await expect(page.getByTestId("release")).toContainText("As built into release");
  await expect(page.getByTestId("mcp-windops")).toContainText("windops.create_draft_work_order");
  await expect(page.getByTestId("mcp-postgres")).toHaveCount(0);
  await noSidewaysScroll(page);
  await page.screenshot({ path: test.info().outputPath("mcp.png"), fullPage: true });
  await tabs.getByRole("link", { name: "Gateway policies" }).click();
  await expect(page.getByTestId("route-web-bff")).toContainText("Keycloak bearer token");
  await expect(page.getByTestId("route-keycloak-login")).toContainText("5 requests each 60 s per address");
  await noSidewaysScroll(page);
  // Each path out of the platform, what it sends and what stops while it is off (EGR-002).
  await tabs.getByRole("link", { name: "Data egress" }).click();
  await expect(page.getByRole("list", { name: "Paths out of the platform" }).getByRole("listitem")).toHaveCount(5, {
    timeout: 30_000,
  });
  await expect(page.getByTestId("egress-claude")).toContainText("Anthropic API");
  await noSidewaysScroll(page);
  await tabs.getByRole("link", { name: "Users" }).click();
  const users = page.getByRole("list", { name: "Users" });
  await expect(users.getByRole("link", { name: operator })).toBeVisible();
  await noSidewaysScroll(page);
  await page.screenshot({ path: test.info().outputPath("users.png"), fullPage: true });

  // A user's page, on any screen size: their MFA, sessions and roles (SRS 9.4).
  await users.getByRole("link", { name: operator }).click();
  await expect(page.getByTestId("mfa-status")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("region", { name: "Sessions" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Roles" })).toBeVisible();
  await noSidewaysScroll(page);

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
  // Ending a user's sessions is a change, made here with the second factor just given.
  const sessions = page.getByRole("region", { name: "Sessions" });
  await sessions.getByRole("button", { name: "End all sessions" }).click();
  await expect(sessions).toContainText("No sessions.");
  await expect(sessions.getByRole("alert")).toHaveCount(0);

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
