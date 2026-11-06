// The SRS 3.2 roles and permissions, for choosing from. tests/test_admin_catalog.py compares this
// file with the Authorization Service's policy, which its own tests compare with the SRS.
// The Authorization Service decides what each grants: this is only the list to pick from.

export const ROLES = [
  { id: "platform-administrator", title: "Platform Administrator" },
  { id: "tenant-administrator", title: "Tenant Administrator" },
  { id: "operations-manager", title: "Operations Manager" },
  { id: "control-room-operator", title: "Control Room Operator" },
  { id: "reliability-engineer", title: "Reliability Engineer" },
  { id: "maintenance-planner", title: "Maintenance Planner" },
  { id: "maintenance-manager", title: "Maintenance Manager" },
  { id: "auditor", title: "Auditor" },
] as const;

/** A tenant's administrators manage these. The platform's own role is granted only in its own tenant. */
export const TENANT_ROLES = ROLES.filter((role) => role.id !== "platform-administrator");

/** The role a form starts on. */
export const FIRST_ROLE = "tenant-administrator";

export const PERMISSIONS = [
  "turbine.read",
  "asset.manage",
  "telemetry.read",
  "alarm.read",
  "alarm.acknowledge",
  "investigation.create",
  "investigation.read",
  "investigation.cancel",
  "finding.review",
  "maintenance.draft",
  "maintenance.submit",
  "maintenance.approve",
  "maintenance.complete",
  "inventory.read",
  "inventory.update",
  "inventory.reserve",
  "weather.read",
  "knowledge.read",
  "knowledge.manage",
  "policy.read",
  "policy.manage",
  "users.manage",
  "roles.manage",
  "permissions.manage",
  "tenant.manage",
  "branding.manage",
  "audit.read",
  "gateway.configure",
  "mcp.configure",
  "evaluation.manage",
] as const;

/** Only the platform holds these, so a tenant's role cannot be given them. */
export const PLATFORM_ONLY: readonly string[] = ["tenant.manage", "gateway.configure", "evaluation.manage"];

export const TENANT_PERMISSIONS = PERMISSIONS.filter((permission) => !PLATFORM_ONLY.includes(permission));

export function roleTitle(id: string): string {
  return ROLES.find((role) => role.id === id)?.title ?? id;
}
