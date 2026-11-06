// The SRS 4.11 screens, and the permission each needs. Hiding one is for convenience only:
// every operation behind it is still checked by the services.
import type { ScopedPermission } from "../api/client";

export interface Screen {
  id: string;
  title: string;
  /** Any one of these shows the screen. Empty means everyone who is signed in. */
  anyOf: string[];
  /** Where it is built, while it is still a placeholder. */
  arrives: string;
}

export const SCREENS: Screen[] = [
  { id: "fleet", title: "Fleet overview", anyOf: ["turbine.read"], arrives: "Phase 4" },
  { id: "telemetry", title: "Telemetry", anyOf: ["telemetry.read"], arrives: "Phase 4" },
  { id: "alarms", title: "Alarm console", anyOf: ["alarm.read"], arrives: "Phase 5" },
  { id: "investigations", title: "Investigations", anyOf: ["investigation.read"], arrives: "Phase 5" },
  { id: "impact", title: "Production impact", anyOf: ["investigation.read"], arrives: "Phase 6" },
  { id: "weather", title: "Weather and sources", anyOf: ["weather.read"], arrives: "Phase 7" },
  { id: "knowledge", title: "Knowledge library", anyOf: ["knowledge.read"], arrives: "Phase 7" },
  { id: "maintenance", title: "Maintenance planner", anyOf: ["maintenance.draft"], arrives: "Phase 9" },
  { id: "parts", title: "Parts lookup", anyOf: ["inventory.read"], arrives: "Phase 9" },
  { id: "approvals", title: "Approval queue", anyOf: ["maintenance.approve"], arrives: "Phase 10" },
  { id: "audit", title: "Audit explorer", anyOf: ["audit.read"], arrives: "Phase 11" },
  { id: "notifications", title: "Notifications", anyOf: [], arrives: "Phase 11" },
  {
    id: "admin",
    title: "Administration",
    anyOf: ["users.manage", "roles.manage", "permissions.manage"],
    arrives: "M3.9",
  },
];

/** A permission held anywhere, for the whole tenant or one farm or turbine, shows its screen. */
export function visibleScreens(granted: ScopedPermission[]): Screen[] {
  const held = new Set(granted.map((item) => item.permission));
  return SCREENS.filter((screen) => screen.anyOf.length === 0 || screen.anyOf.some((p) => held.has(p)));
}

export function screenById(id: string | undefined): Screen | undefined {
  return SCREENS.find((screen) => screen.id === id);
}
