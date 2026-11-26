import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { audienceText, safeLink, validAudience } from "../notifications/api";
import { NotificationScreen } from "../notifications/pages/NotificationScreen";
import { type AuditRecord, filtersFrom, short, validFilters } from "./api";
import { AuditScreen } from "./pages/AuditScreen";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const RECORD: AuditRecord = {
  sequence: 42,
  event_id: "e-42",
  occurred_at: "2026-11-26T06:00:00Z",
  recorded_at: "2026-11-26T06:00:01Z",
  producer: "knowledge",
  action: "knowledge_document.held",
  actor_type: "service",
  actor_id: "knowledge",
  on_behalf_of: "",
  resource_type: "knowledge_document",
  resource_id: "WS-1@1",
  outcome: "succeeded",
  correlation_id: "c-9",
  previous_hash: "a".repeat(64),
  record_hash: "b".repeat(64),
};

/** A fake Web BFF: the first route whose text the URL holds answers, and every call is kept. */
function serve(routes: [string, unknown][]) {
  const asked: { url: string; method: string; body: string }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => {
      asked.push({ url, method: init?.method ?? "GET", body: String(init?.body ?? "") });
      const found = routes.find(([part]) => url.includes(part));
      return Promise.resolve(new Response(JSON.stringify(found ? found[1] : {}), { status: found ? 200 : 404 }));
    }),
  );
  return asked;
}

describe("the audit explorer's filters", () => {
  it("are read from the address, and only what Web BFF accepts is sent", () => {
    expect(filtersFrom(new URLSearchParams("actor_id=u-1&outcome=denied&tab=x&resource_id="))).toEqual({
      actor_id: "u-1",
      outcome: "denied",
    });
    expect(validFilters({ resource_id: "kn:WS-1@1#0", action_prefix: "knowledge_document." })).toBe(true);
    expect(validFilters({ actor_id: "u 1" })).toBe(false);
    expect(validFilters({ outcome: "maybe" })).toBe(false);
    expect(validFilters({ from_time: "not a time" })).toBe(false);
    expect(short("a".repeat(64))).toBe("aaaaaaaa…aaaaaa");
  });
});

describe("the audit explorer", () => {
  it("lists the trail newest first, pivots to the same request, and checks the chain", async () => {
    const asked = serve([
      ["/audit/records", { records: [RECORD], next_page_token: "" }],
      ["/audit/verify", { valid: false, records_checked: 41, first_invalid_sequence: 42 }],
    ]);
    render(
      <MemoryRouter initialEntries={["/?outcome=succeeded"]}>
        <AuditScreen token="token" />
      </MemoryRouter>,
    );
    const records = await screen.findByTestId("audit-records");
    expect(records.textContent).toContain("knowledge_document.held");
    expect(records.textContent).toContain("knowledge_document WS-1@1");
    expect(asked[0]?.url).toContain("outcome=succeeded");
    expect(asked[0]?.url).toContain("newest_first=true");
    fireEvent.click(screen.getByRole("button", { name: "Same request" }));
    await screen.findByDisplayValue("c-9");
    expect(asked.some((a) => a.url.includes("correlation_id=c-9") && !a.url.includes("outcome="))).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Check the chain" }));
    expect((await screen.findByText(/Broken at record #42/)).textContent).toContain("41 intact records");
  });
});

describe("the notification center", () => {
  it("keeps to console links and the audiences the service accepts", () => {
    expect(safeLink("/screens/approvals")).toBe("/screens/approvals");
    expect(safeLink("https://elsewhere.example/screens/x")).toBeNull();
    expect(safeLink("//elsewhere.example")).toBeNull();
    expect(validAudience("case_approved", "holders:maintenance.submit")).toBe(true);
    expect(validAudience("case_approved", "drafter")).toBe(false);
    expect(validAudience("decision_made", "drafter")).toBe(true);
    expect(validAudience("case_approved", "holders:Everyone")).toBe(false);
    expect(audienceText("holders:maintenance.approve")).toBe("Everyone with maintenance.approve");
  });

  it("shows unread ones with their deliveries, marks them read, and lets an administrator edit the rules", async () => {
    const asked = serve([
      ["/notifications/read", { marked: 1 }],
      ["/notifications/rules", { rules: [{ kind: "approval_requested", audience: "holders:maintenance.approve", channels: ["in_app", "email"] }], customized: false }],
      [
        "/notifications",
        {
          unread_count: 1,
          notifications: [
            {
              notification_id: "n-1",
              kind: "approval_requested",
              title: "A work order waits for approval",
              body: "Work order wo_1 was submitted.",
              link: "/screens/approvals",
              case_id: "case_1",
              work_order_id: "wo_1",
              created_at: "2026-11-26T06:00:00Z",
              read_at: null,
              deliveries: [
                { channel: "in_app", status: "delivered", attempts: 1, updated_at: null },
                { channel: "email", status: "recorded", attempts: 1, updated_at: null },
              ],
            },
          ],
        },
      ],
    ]);
    render(
      <MemoryRouter>
        <NotificationScreen token="token" held={new Set(["users.manage"])} />
      </MemoryRouter>,
    );
    const list = await screen.findByTestId("notifications");
    expect(list.textContent).toContain("A work order waits for approval (unread)");
    expect(list.textContent).toContain("Email: recorded");
    fireEvent.click(screen.getByRole("button", { name: "Mark read" }));
    await vi.waitFor(() => expect(asked.some((a) => a.url.endsWith("/notifications/read") && a.body.includes("n-1"))).toBe(true));

    const rules = await screen.findByTestId("notification-rules");
    expect(rules.textContent).toContain("The defaults");
    expect(screen.getByRole("button", { name: "Save the rules" })).toHaveProperty("disabled", true);
    fireEvent.click(screen.getByRole("button", { name: "Add a rule" }));
    const audiences = screen.getAllByLabelText("Audience");
    fireEvent.change(audiences[1] as HTMLElement, { target: { value: "drafter" } });
    expect(rules.textContent).toContain("Not an audience for this kind");
    expect(screen.getByRole("button", { name: "Save the rules" })).toHaveProperty("disabled", true);
    fireEvent.change(audiences[1] as HTMLElement, { target: { value: "requester" } });
    fireEvent.click(screen.getByRole("button", { name: "Save the rules" }));
    await vi.waitFor(() => expect(asked.some((a) => a.method === "PUT" && a.body.includes('"audience":"requester"'))).toBe(true));
  });
});
