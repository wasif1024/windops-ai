import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { adminApi, type PlatformCatalog } from "../api";
import { ApprovalPolicyPage, scopeText } from "./ApprovalPolicyPage";
import { ChangeReviewPage } from "./ChangeReviewPage";
import { GatewayPoliciesPage, McpIntegrationsPage } from "./PlatformCatalogPages";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function serve(answer: (url: string) => unknown) {
  const asked: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      asked.push(url);
      return Promise.resolve(new Response(JSON.stringify(answer(url)), { status: 200 }));
    }),
  );
  return asked;
}

const CATALOG: PlatformCatalog = {
  release: "2026.11.26",
  mcp_servers: [
    {
      server: "windops",
      workload: "windops-mcp-windops",
      calls: "the platform's services",
      boundary: "Tenant-scoped",
      local_only: false,
      tools: [
        {
          name: "windops.create_draft_work_order",
          title: "Draft a work order",
          description: "Drafts a work order.",
          timeout_seconds: 20,
          per_minute: 5,
          per_day: 200,
          external: false,
          read_only: false,
          agents: ["maintenance-planner"],
        },
      ],
    },
    { server: "postgres", workload: "windops-mcp-postgres", calls: "statistics", boundary: "Read only", local_only: true, tools: [] },
  ],
  gateway_routes: [
    {
      name: "web-bff",
      purpose: "The browser's API",
      uris: ["/api/*"],
      methods: ["any"],
      sign_in: "Keycloak bearer token",
      limit: { count: 600, window_seconds: 60, per: "address", rejected_code: 429 },
      cors: { origins: "the console's own origins only", methods: "GET,POST", credentials: false },
      timeouts_seconds: { connect: 2, send: 10, read: 15 },
      upstream: ["windops-web-bff"],
      upstream_tls: true,
      health_checked: true,
      traced: true,
    },
  ],
  api_limits: { window_seconds: 60, per_user: [{ traffic: "standard", requests: 120 }] },
};

describe("the approval policy", () => {
  it("names the roles that approve and who holds them, flagging a grant to someone gone", async () => {
    serve((url) => {
      if (url.includes("/admin/roles/maintenance-manager/permissions"))
        return { role: "maintenance-manager", permissions: ["maintenance.approve"], customized: false };
      if (url.includes("/admin/roles/")) return { role: "x", permissions: ["turbine.read"], customized: false };
      if (url.includes("/admin/role-grants"))
        return {
          grants: [
            { grant_id: "g1", user_id: "u-1", role: "maintenance-manager", wind_farm_id: "", turbine_id: "", granted_by: "", granted_at: null, revoked_by: "", revoked_at: null },
            { grant_id: "g2", user_id: "u-gone", role: "maintenance-manager", wind_farm_id: "dogger-east", turbine_id: "", granted_by: "", granted_at: null, revoked_by: "", revoked_at: null },
            { grant_id: "g3", user_id: "u-2", role: "auditor", wind_farm_id: "", turbine_id: "", granted_by: "", granted_at: null, revoked_by: "", revoked_at: null },
          ],
        };
      return { changes: [] };
    });
    render(
      <MemoryRouter>
        <ApprovalPolicyPage api={adminApi("t")} emails={new Map([["u-1", "manager@north-sea.example"]])} />
      </MemoryRouter>,
    );
    const policy = await screen.findByTestId("approval-policy");
    expect(screen.getByRole("list", { name: "Roles that approve" }).textContent).toBe("Maintenance Manager");
    const approvers = screen.getByTestId("approvers").textContent ?? "";
    expect(approvers).toContain("manager@north-sea.example");
    expect(approvers).toContain("u-gone");
    expect(approvers).toContain("no longer a user here");
    expect(approvers).not.toContain("u-2");
    expect(policy.textContent).toContain("No agent ever approves");
    expect(scopeText("dogger-east", "")).toBe("wind farm dogger-east");
  });
});

describe("change review", () => {
  it("lists a kind of change from the audit trail, newest first, by who made it", async () => {
    const asked = serve(() => ({
      records: [
        { sequence: 9, event_id: "e", occurred_at: null, recorded_at: null, producer: "authorization", action: "authorization.role.granted.auditor", actor_type: "user", actor_id: "u-1", on_behalf_of: "", resource_type: "user", resource_id: "u-2", outcome: "succeeded", correlation_id: "", previous_hash: "", record_hash: "" },
      ],
      next_page_token: "",
    }));
    render(
      <MemoryRouter>
        <ChangeReviewPage token="t" emails={new Map([["u-1", "admin@north-sea.example"]])} />
      </MemoryRouter>,
    );
    await screen.findByTestId("changes");
    expect(asked[0]).toContain("action_prefix=identity.user.");
    fireEvent.click(screen.getByRole("button", { name: "Roles" }));
    expect((await screen.findByTestId("changes")).textContent).toContain("by admin@north-sea.example");
    expect(asked.some((u) => /action_prefix=authorization\.role\.(&|$)/.test(u) && u.includes("newest_first=true"))).toBe(true);
    expect(screen.getByRole("link", { name: "See these in the audit explorer" }).getAttribute("href")).toContain("authorization.role.");
  });
});

describe("MCP integrations and gateway policies", () => {
  it("show what this release holds, development-only servers on request", async () => {
    serve(() => CATALOG);
    render(<McpIntegrationsPage api={adminApi("t")} />);
    expect((await screen.findByTestId("release")).textContent).toContain("2026.11.26");
    const windops = screen.getByTestId("mcp-windops");
    expect(windops.textContent).toContain("Writes");
    expect(windops.textContent).toContain("agents: maintenance-planner");
    expect(screen.queryByTestId("mcp-postgres")).toBeNull();
    fireEvent.click(screen.getByLabelText("Show development-only servers"));
    expect(screen.getByTestId("mcp-postgres").textContent).toContain("Read only");
    cleanup();
    render(<GatewayPoliciesPage api={adminApi("t")} />);
    const route = await screen.findByTestId("route-web-bff");
    expect(route.textContent).toContain("600 requests each 60 s per address, then 429");
    expect(route.textContent).toContain("never with credentials");
    expect(screen.getByTestId("api-limits").textContent).toContain("Standard: 120 requests");
  });
});
