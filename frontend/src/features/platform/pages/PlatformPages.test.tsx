import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PlatformApi, Tenant } from "../api";
import { BreakGlassPage } from "./BreakGlassPage";
import { TenantPage } from "./TenantPage";
import { TenantsPage } from "./TenantsPage";

afterEach(cleanup);

const atlas: Tenant = { tenant_id: "atlas-wind", name: "Atlas Wind", status: "active", created_at: null };
const platform: Tenant = { tenant_id: "windops-platform", name: "WindOps Platform", status: "active", created_at: null };

function fakeApi(overrides: Partial<PlatformApi> = {}): PlatformApi {
  return {
    tenants: vi.fn(async () => ({ tenants: [atlas, platform], next_page_token: "" })),
    tenant: vi.fn(async () => atlas),
    branding: vi.fn(async () => ({
      tenant_id: "atlas-wind",
      display_name: "Atlas",
      primary_color: "#0b5394",
      accent_color: "#38761d",
      login_title: "",
      login_message: "",
      time_zone: "Europe/Oslo",
      locale: "en-GB",
      has_logo: false,
      updated_at: null,
      custom_domain: { domain: "ops.atlas.example", status: "pending" },
    })),
    breakGlass: vi.fn(async () => ({ grants: [] })),
    ...overrides,
  } as unknown as PlatformApi;
}

function show(page: React.ReactNode) {
  render(<MemoryRouter>{page}</MemoryRouter>);
}

describe("the Platform console", () => {
  it("lists tenants, each linking to its own page", async () => {
    show(<TenantsPage api={fakeApi()} />);
    const link = await screen.findByRole("link", { name: /atlas-wind/ });
    expect(link.getAttribute("href")).toBe("/?tab=tenants&tenant=atlas-wind");
  });

  it("refuses a tenant ID that would not be valid in a token, before sending", async () => {
    const createTenant = vi.fn();
    show(<TenantsPage api={fakeApi({ createTenant })} />);
    fireEvent.change(screen.getByLabelText("Tenant ID"), { target: { value: "Not Valid" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Create tenant" }));
    expect(await screen.findByText(/lowercase letters, digits and hyphens/)).toBeTruthy();
    expect(createTenant).not.toHaveBeenCalled();
  });

  it("needs a reason to suspend, and shows branding read-only with no proof token", async () => {
    const suspend = vi.fn();
    show(<TenantPage api={fakeApi({ suspend })} id="atlas-wind" />);
    fireEvent.change(await screen.findByLabelText(/Reason for suspending/), { target: { value: "short" } });
    fireEvent.click(screen.getByRole("button", { name: "Suspend tenant" }));
    expect(await screen.findByText(/at least 10 characters/)).toBeTruthy();
    expect(suspend).not.toHaveBeenCalled();
    expect((await screen.findByTestId("tenant-domain")).textContent).toBe("ops.atlas.example (pending)");
    expect(screen.queryByRole("button", { name: /Save/ })).toBeNull();
  });

  it("offers break-glass only into customer tenants, and needs a real reason", async () => {
    const startBreakGlass = vi.fn();
    show(<BreakGlassPage api={fakeApi({ startBreakGlass })} />);
    const options = (await screen.findByLabelText("Tenant")).querySelectorAll("option");
    expect([...options].map((option) => option.value)).toEqual(["atlas-wind"]);
    fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "looking" } });
    fireEvent.click(screen.getByRole("button", { name: "Start access" }));
    expect(await screen.findByText(/at least 20 characters/)).toBeTruthy();
    expect(startBreakGlass).not.toHaveBeenCalled();
  });
});
