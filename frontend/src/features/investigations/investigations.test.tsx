import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MaintenanceApi, WorkOrder } from "../maintenance/api";
import { CompleteWork, parseParts } from "../maintenance/pages/Completion";
import {
  type CaseDetail,
  citationsOf,
  type EvidenceSnapshot,
  type FindingReview,
  type InvestigationApi,
  openConcerns,
  type Report,
  sourceOf,
} from "./api";
import { EvidenceViewer } from "./pages/EvidenceViewer";
import { CasePage, describe as describeEvent } from "./pages/CasePage";
import { sectionsOf } from "./pages/Findings";
import { follow, parseSse } from "./stream";

afterEach(cleanup);

const REPORT: Report = {
  run_id: "run_1",
  report: {
    observed_facts: [{ statement: "Oil at 85 °C", citations: ["tsnap_1"] }],
    hypotheses: [{ statement: "A stuck thermostatic valve", support: ["tsnap_1", "kn:GBX-OIL-01@3#12"], confidence: 0.6 }],
    data_gaps: ["No oil sample"],
    recommended_actions: [{ action: "inspection", summary: "Inspect the valve" }],
    confidence: 0.6,
    explanation: "The oil runs hot under load",
  },
  reviews: [],
};

function review(finding: string, action: FindingReview["action"], note = "", id = finding + action): FindingReview {
  return { review_id: id, run_id: "run_1", finding, action, note, reviewed_by: "u-engineer", reviewed_at: "2026-11-26T08:00:00Z" };
}

const DETAIL: CaseDetail = {
  investigation: {
    case_id: "case_1",
    turbine_id: "dogger-east-t01",
    title: "Gearbox oil hot",
    status: "verified",
    severity: "critical",
    investigation_status: "completed",
    run_id: "run_1",
    created_at: "2026-11-26T07:00:00Z",
    updated_at: "2026-11-26T08:00:00Z",
    events: "/api/v1/investigations/case_1/events",
  },
  origin: "detection",
  created_by: "service:fault-detection",
  version: 14,
  evidence_snapshot_id: "evs_1",
  evidence_version: 1,
  evidence_status: "ready",
  stale_rule: "",
  stale_detail: "",
  detections: [{ detection_id: "d1", rule_id: "gearbox-oil-critical", rule_version: "1", severity: "critical", attached_at: null }],
  history: [{ at: "2026-11-26T07:00:00Z", action: "created", actor_type: "service", actor_id: "service:fault-detection", note: "" }],
  verifications: [
    {
      work_order_id: "wo_1",
      proposal_version: 1,
      completed_by: "u-planner",
      completed_at: "2026-11-26T07:50:00Z",
      status: "reported",
      outcome: "resolved",
      window_start: "2026-11-26T07:50:00Z",
      window_end: "2026-11-26T07:52:00Z",
      after_samples: 120,
      metrics: [
        {
          metric: "METRIC_GEARBOX_TEMPERATURE",
          unit: "degC",
          before_mean: 85,
          after_mean: 50,
          before_maximum: 86,
          after_maximum: 51,
          before_readings: 200,
          after_readings: 120,
        },
      ],
      rules: [],
      note: "None of the case's rules finds anything after the work",
      reported_at: "2026-11-26T07:53:00Z",
      review: "",
      reviewed_by: "",
      reviewed_at: null,
      review_reason: "",
    },
  ],
};

function stream(chunks: string[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

function event(sequence: number, kind: string, extra: Record<string, unknown> = {}): string {
  const data = { sequence, at: null, kind, run_id: "run_1", position: 0, total_steps: 0, step: "", status: "", note: "", ...extra };
  return `id: ${sequence}\nevent: ${kind}\ndata: ${JSON.stringify(data)}\n\n`;
}

describe("the progress stream", () => {
  it("splits whole messages, keeping a half one for later, and skips comments", () => {
    const { messages, rest } = parseSse("retry: 3000\n\n: still investigating\n\nid: 1\nevent: step\ndata: {}\n\nid: 2\nda");
    expect(messages).toEqual([{ id: "1", event: "step", data: "{}" }]);
    expect(rest).toBe("id: 2\nda");
  });

  it("follows until finished, carrying on after the last event it saw, each once", async () => {
    const answers = [
      stream([event(1, "started"), event(2, "step", { position: 1, total_steps: 2, step: "plan" }).slice(0, 20)]),
      stream([event(2, "step", { position: 1, total_steps: 2, step: "plan" }), event(3, "run_ended", { status: "completed" }), "event: finished\ndata: {}\n\n"]),
    ];
    const fetcher = vi.fn(() => Promise.resolve(answers.shift() as Response));
    const seen: number[] = [];
    const last = await follow("case_1", "token", (e) => seen.push(e.sequence), new AbortController().signal, fetcher, 0);
    expect(seen).toEqual([1, 2, 3]);
    expect(last).toBe(3);
    const second = fetcher.mock.calls[1] as unknown as [string, RequestInit];
    expect((second[1].headers as Record<string, string>)["Last-Event-ID"]).toBe("1");
    expect((second[1].headers as Record<string, string>).Authorization).toBe("Bearer token");
  });

  it("says each event in a line", () => {
    const step = { sequence: 4, at: null, kind: "step", run_id: "r", position: 3, total_steps: 18, step: "gather_evidence", status: "", note: "" };
    expect(describeEvent(step)).toBe("Step 3 of 18: Gather evidence");
  });
});

describe("findings and their reviews", () => {
  it("names each finding by where it is in the report", () => {
    const names = sectionsOf(REPORT.report ?? {}).flatMap((s) => s.items.map((i) => i.name));
    expect(names).toEqual(["observed_facts[0]", "hypotheses[0]", "data_gaps[0]", "recommended_actions[0]"]);
  });

  it("an approver is told of findings still challenged or rejected, not those accepted since", () => {
    const reviews = [
      review("hypotheses[0]", "challenge", "Replaced last month", "a"),
      review("hypotheses[0]", "annotate", "Asked the site", "b"),
      review("data_gaps[0]", "reject", "A sample was taken", "c"),
      review("observed_facts[0]", "challenge", "Sensor drift", "d"),
      review("observed_facts[0]", "accept", "", "e"),
    ];
    expect(openConcerns(reviews).map((r) => r.finding)).toEqual(["hypotheses[0]", "data_gaps[0]"]);
  });

  it("reads parts used as PART x2, a missing quantity one", () => {
    expect(parseParts("GBX-FAN-CAP x2, P-1")).toEqual([
      { part_number: "GBX-FAN-CAP", quantity: 2 },
      { part_number: "P-1", quantity: 1 },
    ]);
    expect(parseParts("")).toEqual([]);
    expect(parseParts("two words x2")).toBeNull();
  });
});

const SNAPSHOT: EvidenceSnapshot = {
  snapshot_id: "evs_1",
  case_id: "case_1",
  version: 1,
  turbine_id: "dogger-east-t01",
  content_hash: "h".repeat(64),
  hash_format: "windops-evidence-snapshot-v1",
  created_at: "2026-11-26T07:00:00Z",
  created_by: "service:investigation",
  window_start: "2026-11-26T05:00:00Z",
  window_end: "2026-11-26T07:00:00Z",
  item_count: 2,
  verified: true,
  items: [
    {
      source_service: "telemetry",
      record_type: "telemetry_snapshot",
      record_id: "tsnap_1",
      version: "c".repeat(64),
      window_start: null,
      window_end: null,
      retrieved_at: null,
      data_quality: { quality: { ok: 90, delayed: 10 }, gaps: 3, rejected: 1, samples: 100 },
      content: { sample_count: 100 },
      content_hash: "d".repeat(64),
    },
    {
      source_service: "fault-detection",
      record_type: "detection",
      record_id: "det_1",
      version: "1",
      window_start: null,
      window_end: null,
      retrieved_at: null,
      data_quality: {},
      content: { rule_id: "gearbox-oil-critical", severity: "critical", amount: 85 },
      content_hash: "e".repeat(64),
    },
  ],
};

function fakeApi(): InvestigationApi & { reviewed: unknown[]; answered: unknown[]; frozen: number } {
  const reviewed: unknown[] = [];
  const answered: unknown[] = [];
  const empty = new Response(new ReadableStream({ start: (c) => c.close() }), { status: 200 });
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(empty)));
  const api = {
    token: "token",
    reviewed,
    answered,
    frozen: 0,
    snapshots: () => Promise.resolve([SNAPSHOT]),
    snapshot: () => Promise.resolve(SNAPSHOT),
    freeze: () => {
      api.frozen += 1;
      return Promise.resolve({ ...SNAPSHOT, snapshot_id: "evs_2", version: 2 });
    },
    list: () => Promise.resolve([DETAIL.investigation]),
    detail: () => Promise.resolve(DETAIL),
    report: () => Promise.resolve(REPORT),
    start: () => Promise.resolve(DETAIL.investigation),
    review: (...args: unknown[]) => {
      reviewed.push(args.slice(2));
      return Promise.resolve(review(args[2] as string, args[3] as FindingReview["action"]));
    },
    answer: (...args: unknown[]) => {
      answered.push(args.slice(2));
      return Promise.resolve(DETAIL);
    },
  };
  return api;
}

describe("the case page", () => {
  it("shows the findings and the verification, and a reviewer challenges and closes", async () => {
    const api = fakeApi();
    render(
      <MemoryRouter>
        <CasePage api={api} caseId="case_1" canReview />
      </MemoryRouter>,
    );
    await screen.findByText("A stuck thermostatic valve");
    expect(screen.getByTestId("verification").textContent).toContain("Resolved");
    expect(screen.getByText("85 degC")).toBeTruthy();

    const finding = screen.getByTestId("finding-hypotheses[0]");
    fireEvent.click(finding.querySelector("button") as HTMLButtonElement);
    const challenge = screen.getByRole("button", { name: "Challenge" }) as HTMLButtonElement;
    expect(challenge.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Note on hypotheses[0]"), { target: { value: "Replaced last month" } });
    fireEvent.click(challenge);
    await waitFor(() => expect(api.reviewed).toEqual([["hypotheses[0]", "challenge", "Replaced last month"]]));

    fireEvent.click(screen.getByRole("button", { name: "Close the case" }));
    await waitFor(() => expect(api.answered).toEqual([["close", "", 14]]));
  });

  it("someone who may only read sees no review or close buttons", async () => {
    render(
      <MemoryRouter>
        <CasePage api={fakeApi()} caseId="case_1" canReview={false} />
      </MemoryRouter>,
    );
    await screen.findByText("A stuck thermostatic valve");
    expect(screen.queryByRole("button", { name: "Review" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Close the case" })).toBeNull();
  });
});

describe("recording the work done", () => {
  it("sends what was done, and refuses an end in the future", async () => {
    const sent: unknown[] = [];
    const api = {
      complete: (...args: unknown[]) => {
        sent.push(args);
        return Promise.resolve({});
      },
    } as unknown as MaintenanceApi;
    const order = { work_order_id: "wo_1", revision: 6 } as WorkOrder;
    const done = vi.fn();
    render(<CompleteWork api={api} order={order} onDone={done} />);
    const record = screen.getByRole("button", { name: "Record as done" }) as HTMLButtonElement;
    expect(record.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("What was done and found"), { target: { value: "Replaced the valve" } });
    fireEvent.change(screen.getByLabelText("Parts used"), { target: { value: "GBX-VALVE x1" } });
    fireEvent.change(screen.getByLabelText("Actual downtime, hours"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("Turbine handed back"), { target: { value: "2999-01-01T10:00" } });
    expect(screen.getByText("The work cannot end in the future.")).toBeTruthy();
    expect(record.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Turbine handed back"), { target: { value: "2026-01-01T10:00" } });
    fireEvent.change(screen.getByLabelText("Work started"), { target: { value: "2026-01-01T08:00" } });
    fireEvent.click(record);
    await waitFor(() => expect(done).toHaveBeenCalled());
    const [[id, , revision, body]] = sent as [[string, string, number, Record<string, unknown>]];
    expect([id, revision]).toEqual(["wo_1", 6]);
    expect(body).toMatchObject({
      summary: "Replaced the valve",
      parts_used: [{ part_number: "GBX-VALVE", quantity: 1 }],
      actual_downtime_hours: 2,
    });
    expect(new Date(body.completed_at as string).getTime()).toBe(new Date("2026-01-01T10:00").getTime());
  });
});

describe("the evidence viewer", () => {
  it("shows each item with who cites it, the data quality, and citations beyond the snapshot", async () => {
    const cited = citationsOf(REPORT.report);
    expect(cited.get("tsnap_1")).toEqual(["observed_facts[0]", "hypotheses[0]"]);
    expect(sourceOf("kn:GBX-OIL-01@3#12")).toBe("Knowledge library");
    const api = fakeApi();
    render(
      <MemoryRouter>
        <EvidenceViewer api={api} caseId="case_1" snapshotId="evs_1" canFreeze />
      </MemoryRouter>,
    );
    await screen.findByText("Hashes match");
    const telemetry = screen.getByTestId("evidence-telemetry_snapshot");
    expect(telemetry.textContent).toContain("Cited by observed_facts[0], hypotheses[0]");
    expect(screen.getByTestId("data-quality").textContent).toContain("90% of 100 readings checked as good");
    expect(screen.getByTestId("data-quality").textContent).toContain("3 missing in gaps");
    expect(screen.getByTestId("evidence-detection").textContent).toContain("Not cited by a finding");
    expect(screen.getByTestId("other-citations").textContent).toContain("Knowledge library");
    fireEvent.click(screen.getByRole("button", { name: "Freeze new evidence" }));
    await waitFor(() => expect(api.frozen).toBe(1));
  });

  it("someone who may not freeze sees no button, and a closed case offers none", async () => {
    render(
      <MemoryRouter>
        <EvidenceViewer api={fakeApi()} caseId="case_1" snapshotId={null} canFreeze={false} />
      </MemoryRouter>,
    );
    await screen.findByText("Hashes match");
    expect(screen.queryByRole("button", { name: "Freeze new evidence" })).toBeNull();
  });
});
