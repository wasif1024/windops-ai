import { describe, expect, it } from "vitest";
import { visibleTabs } from "./AdminPage";

describe("the administrator console's tabs", () => {
  it("shows a Tenant Administrator every tab", () => {
    const held = new Set(["users.manage", "roles.manage", "permissions.manage", "audit.read", "branding.manage"]);
    expect(visibleTabs(held).map((tab) => tab.id)).toEqual(["users", "roles", "denials", "branding"]);
  });

  it("shows only what a permission allows", () => {
    expect(visibleTabs(new Set(["permissions.manage"])).map((tab) => tab.id)).toEqual(["roles"]);
    expect(visibleTabs(new Set())).toEqual([]);
  });
});
