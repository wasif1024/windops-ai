import { describe, expect, it } from "vitest";
import { SCREENS, visibleScreens } from "./navigation";

const grant = (permission: string, wind_farm_id = "") => ({ permission, wind_farm_id, turbine_id: "" });

describe("role-aware navigation", () => {
  it("shows only notifications to someone with no permissions", () => {
    expect(visibleScreens([]).map((s) => s.id)).toEqual(["notifications"]);
  });

  it("shows a control room operator their screens, and no administration", () => {
    const operator = ["turbine.read", "telemetry.read", "alarm.read", "alarm.acknowledge"].map((p) => grant(p));
    const ids = visibleScreens(operator).map((s) => s.id);
    expect(ids).toEqual(expect.arrayContaining(["fleet", "telemetry", "alarms"]));
    expect(ids).not.toContain("admin");
    expect(ids).not.toContain("approvals");
  });

  it("counts a permission held for one wind farm", () => {
    expect(visibleScreens([grant("alarm.read", "farm-1")]).map((s) => s.id)).toContain("alarms");
  });

  it("shows administration for any administrative permission", () => {
    expect(visibleScreens([grant("branding.manage")]).map((s) => s.id)).toContain("admin");
  });

  it("names every screen once", () => {
    expect(new Set(SCREENS.map((s) => s.id)).size).toBe(SCREENS.length);
  });
});
