// Recording approved work done, and what was recorded (FR-MNT-004, M10.6). Recording it starts
// the case's verification: after a window, the turbine's telemetry is compared with its evidence
// and the fault rules run again (SRS 5.4 step 15).
import { useState } from "react";
import { useChange } from "../../../api/useChange";
import { Button } from "../../../components/Button";
import { ChangeStatus } from "../../../components/ChangeStatus";
import { INPUT, LABEL, when } from "../../../components/Panel";
import { titleCase } from "../../fleet/format";
import type { Completion, CompletionIn, MaintenanceApi, PartLine, WorkOrder } from "../api";

/** A datetime-local input's value for a moment, in the browser's own time zone. */
function local(at: Date): string {
  const shifted = new Date(at.getTime() - at.getTimezoneOffset() * 60_000);
  return shifted.toISOString().slice(0, 16);
}

/** "GBX-FAN-CAP x2, P-1" into parts: a missing quantity is one. */
export function parseParts(text: string): PartLine[] | null {
  const parts: PartLine[] = [];
  for (const entry of text.split(",").map((item) => item.trim()).filter(Boolean)) {
    const found = /^(\S+?)(?:\s*[x×]\s*(\d{1,4}))?$/i.exec(entry);
    if (!found?.[1]) return null;
    const quantity = found[2] ? Number(found[2]) : 1;
    if (quantity < 1) return null;
    parts.push({ part_number: found[1], quantity });
  }
  return parts;
}

export function CompleteWork({ api, order, onDone }: { api: MaintenanceApi; order: WorkOrder; onDone: () => void }) {
  const change = useChange();
  // The clock, read once as the form opens: the latest the work may end, and the defaults.
  const [opened] = useState(() => Date.now());
  const [started, setStarted] = useState(() => local(new Date(opened - 2 * 3_600_000)));
  const [completed, setCompleted] = useState(() => local(new Date(opened)));
  const [summary, setSummary] = useState("");
  const [downtime, setDowntime] = useState("0");
  const [partsText, setPartsText] = useState("");
  const parts = parseParts(partsText);
  const startedAt = new Date(started);
  const completedAt = new Date(completed);
  const hours = Number(downtime);
  const problem =
    !summary.trim()
      ? "Say what was done."
      : !(startedAt < completedAt)
        ? "The work starts before it ends."
        : completedAt.getTime() > opened + 30 * 60_000
          ? "The work cannot end in the future."
          : !(hours >= 0)
            ? "Downtime is a number of hours."
            : parts === null
              ? "Write parts as PART-NUMBER x2, separated by commas."
              : "";
  const busy = change.state.kind === "busy";
  const done: CompletionIn = {
    started_at: startedAt.toISOString(),
    completed_at: completedAt.toISOString(),
    summary: summary.trim(),
    parts_used: parts ?? [],
    actual_downtime_hours: hours,
  };
  return (
    <form
      className="space-y-3"
      aria-label="Record the work done"
      onSubmit={(event) => {
        event.preventDefault();
        if (problem) return;
        void change.run((key) => api.complete(order.work_order_id, key, order.revision, done)).then((made) => made && onDone());
      }}
    >
      <p className="text-sm text-slate-600">
        Once recorded, the turbine is watched for the verification window, then compared with the case's evidence.
      </p>
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <label className={LABEL} htmlFor="work-started">
            Work started
          </label>
          <input id="work-started" type="datetime-local" className={INPUT} value={started} onChange={(e) => setStarted(e.target.value)} />
        </div>
        <div>
          <label className={LABEL} htmlFor="work-completed">
            Turbine handed back
          </label>
          <input id="work-completed" type="datetime-local" className={INPUT} value={completed} onChange={(e) => setCompleted(e.target.value)} />
        </div>
      </div>
      <label className={LABEL} htmlFor="work-summary">
        What was done and found
      </label>
      <textarea id="work-summary" className={INPUT} rows={3} maxLength={2000} value={summary} onChange={(e) => setSummary(e.target.value)} />
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <label className={LABEL} htmlFor="work-parts">
            Parts used
          </label>
          <input id="work-parts" className={INPUT} placeholder="GBX-FAN-CAP x2" value={partsText} onChange={(e) => setPartsText(e.target.value)} />
        </div>
        <div>
          <label className={LABEL} htmlFor="work-downtime">
            Actual downtime, hours
          </label>
          <input id="work-downtime" type="number" min={0} step={0.5} className={INPUT} value={downtime} onChange={(e) => setDowntime(e.target.value)} />
        </div>
      </div>
      {problem && summary && <p className="text-sm text-amber-800">{problem}</p>}
      <Button type="submit" disabled={busy || Boolean(problem)}>
        Record as done
      </Button>
      <ChangeStatus state={change.state} onDismiss={change.reset} />
    </form>
  );
}

const OUTCOME_TEXT: Record<string, string> = {
  resolved: "Resolved: the case's fault rules no longer find it.",
  not_resolved: "Not resolved: the case's fault rules still find it.",
  inconclusive: "Inconclusive: there was too little data to say.",
};

export function CompletionView({ completion, outcome }: { completion: Completion; outcome: string }) {
  return (
    <div className="space-y-2 text-sm" data-testid="completion">
      <p className="whitespace-pre-line break-words text-slate-800">{completion.summary}</p>
      <dl className="grid gap-x-4 gap-y-1 sm:grid-cols-2">
        <dt className="text-slate-500">Work</dt>
        <dd className="break-words">
          {when(completion.started_at)} to {when(completion.completed_at)}
        </dd>
        <dt className="text-slate-500">Downtime</dt>
        <dd>{completion.actual_downtime_hours} hours</dd>
        <dt className="text-slate-500">Parts used</dt>
        <dd className="break-words">
          {completion.parts_used.map((p) => `${p.part_number} ×${p.quantity}`).join(", ") || "None"}
        </dd>
        <dt className="text-slate-500">Recorded</dt>
        <dd className="break-words">
          by {completion.recorded_by} {when(completion.recorded_at)}
        </dd>
      </dl>
      <p className="font-medium text-slate-800" data-testid="verification-outcome">
        {outcome ? (OUTCOME_TEXT[outcome] ?? titleCase(outcome)) : "Waiting for its verification."}
      </p>
    </div>
  );
}
