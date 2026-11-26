// The Phase 11 gate (M11.8): role-based browser tests cover every SRS 4.11 screen. Each journey
// names the screens it covers and the role it runs as, with `covers`. After a run,
// scripts/checks/gates/phase_11.py reads Playwright's JSON report and passes only when every
// screen below was covered by a passing journey on a desktop and on a phone (UX-001).
import { expect, type Page, type TestDetails } from "@playwright/test";
import MENUS from "./role-menus.json" with { type: "json" };

/** Each screen SRS 4.11 lists, by an ID the gate reports on. */
export const SRS_4_11 = {
  "fleet-overview": "Fleet overview",
  "turbine-detail": "Turbine detail",
  "telemetry-charts": "Telemetry charts",
  "alarm-console": "Alarm console",
  "investigation-list": "Investigation list",
  "investigation-timeline": "Investigation timeline",
  "live-progress": "Live progress",
  findings: "Findings",
  "evidence-viewer": "Evidence viewer",
  "production-impact": "Production impact view",
  "weather-freshness": "Weather and source freshness panel",
  "maintenance-planner": "Maintenance planner",
  "parts-lookup": "Parts lookup",
  "approval-queue": "Approval queue",
  "knowledge-library": "Knowledge library",
  "audit-explorer": "Audit explorer",
  "notification-center": "Notification center",
  "admin-users": "Administrator console: users",
  "admin-roles": "Administrator console: roles",
  "admin-permissions": "Administrator console: permissions",
  "admin-mfa": "Administrator console: MFA",
  "admin-sessions": "Administrator console: sessions",
  "admin-branding": "Administrator console: branding",
  "admin-approval-policies": "Administrator console: approval policies",
  "admin-mcp-integrations": "Administrator console: MCP integrations",
  "admin-gateway-policies": "Administrator console: gateway policies",
  "platform-tenants": "Platform console: list, create and suspend tenants",
  "platform-branding-domains":
    "Platform console: tenants' branding and domains",
  "platform-break-glass": "Platform console: break-glass access",
} as const;

export type ScreenId = keyof typeof SRS_4_11;
export type Role = keyof typeof MENUS;

/** A journey's details: the role it signs in as, and the SRS 4.11 screens it covers. */
export function covers(role: Role, ...screens: ScreenId[]): TestDetails {
  return {
    annotation: [
      { type: "role", description: role },
      ...screens.map((id) => ({ type: "screen", description: id })),
    ],
  };
}

/** The role's menu shows exactly the screens SRS 3.2 and the console's navigation give it. */
export async function expectMenu(
  page: Page,
  isMobile: boolean,
  role: Role,
): Promise<void> {
  if (isMobile)
    await page.getByRole("button", { name: "Open the menu" }).click();
  const menu = isMobile
    ? page.getByRole("dialog", { name: "Screens" })
    : page.getByRole("navigation", { name: "Screens" });
  // A link's name can end with its unread count, which is not part of the screen's title.
  const names = (await menu.getByRole("link").allInnerTexts()).map((text) =>
    text.replace(/\s*\d+\+?\s*(unread)?\s*$/, "").trim(),
  );
  expect(names, `${role}'s menu`).toEqual(MENUS[role]);
  if (isMobile) await page.keyboard.press("Escape");
}
