import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PendingChange } from "../api";
import { isHeld } from "../api";
import { PolicyChangesPage, type PolicyChangesApi } from "./PolicyChangesPage";

const held: PendingChange = {
  change_id: "0f8b2c1e-1111-4222-8333-444455556666",
  tenant_id: "north-sea",
  kind: "grant",
  summary: "Give u-2 the maintenance-manager role, in the whole tenant",
  proposed_by: "u-1",
  proposed_at: "2026-11-21T09:00:00Z",
  expires_at: "2026-11-28T09:00:00Z",
  status: "pending",
  decided_by: "",
  decided_at: null,
  decline_reason: "",
  escalated: true,
  role: "maintenance-manager",
  grantee_id: "u-2",
  permissions: [],
};

afterEach(cleanup);

describe("approval policy changes", () => {
  it("tells a held change from one made", () => {
    expect(isHeld(held)).toBe(true);
    expect(isHeld({ grant_id: "g-1" })).toBe(false);
  });

  it("confirms a pending change, and declines only with a reason", async () => {
    const api = {
      policyChanges: vi.fn(async () => ({ changes: [held] })),
      confirmChange: vi.fn(async () => ({ ...held, status: "confirmed" as const })),
      declineChange: vi.fn(async () => ({ ...held, status: "declined" as const })),
    };
    render(<PolicyChangesPage api={api as PolicyChangesApi} title="Changes" empty="None" />);
    const item = await screen.findByTestId(`change-${held.change_id}`);
    expect(item.textContent).toContain("maintenance-manager");
    expect(item.textContent).toContain("Escalated");
    expect((screen.getByRole("button", { name: "Decline" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(api.confirmChange).toHaveBeenCalledWith(expect.any(String), held));
  });
});
