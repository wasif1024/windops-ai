// A case's evidence, as frozen (SRS 4.11, FR-INV-002, M11.2): every item with its hash, the
// telemetry's data quality, which findings cite each item, and the report's other citations. A
// person may freeze a new version, which re-evaluates the case (EVD-008).
import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { useChange } from "../../../api/useChange";
import { useLoad } from "../../../api/useLoad";
import { Button } from "../../../components/Button";
import { ChangeStatus } from "../../../components/ChangeStatus";
import { Loading, Panel, when } from "../../../components/Panel";
import { StatusBadge } from "../../fleet/Freshness";
import { citationsOf, type EvidenceItem, type EvidenceSnapshot, type InvestigationApi, sourceOf } from "../api";

const GROUPS: { type: string; title: string }[] = [
  { type: "turbine", title: "Turbine" },
  { type: "telemetry_snapshot", title: "Telemetry" },
  { type: "alarm", title: "Alarms" },
  { type: "detection", title: "Detections" },
  { type: "fault_rule_version", title: "Fault rule versions" },
];

function text(value: unknown): string {
  if (value === undefined || value === null || value === "") return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** The few facts that say what an item is, by its type. */
export function factsOf(item: EvidenceItem): [string, string][] {
  const c = item.content;
  const model = (c.model ?? {}) as Record<string, unknown>;
  const facts: [string, unknown][] =
    item.record_type === "turbine"
      ? [["Name", c.name], ["Status", c.status], ["Model", model.model], ["Wind farm", c.wind_farm_id]]
      : item.record_type === "telemetry_snapshot"
        ? [["Samples", c.sample_count], ["From", c.from_time], ["To", c.to_time]]
        : item.record_type === "alarm"
          ? [["Code", c.code], ["Severity", c.severity], ["State", c.state], ["Raised", c.raised_at]]
          : item.record_type === "detection"
            ? [["Rule", c.rule_id], ["Severity", c.severity], ["Figure", c.amount], ["Found", c.summary]]
            : item.record_type === "fault_rule_version"
              ? [["Rule", c.rule_id], ["Version", c.version], ["Kind", c.kind]]
              : [];
  return facts.map(([label, value]) => [label, text(value)] as [string, string]).filter(([, value]) => value);
}

/** The findings citing this item: by its ID, its snapshot's, or an ID inside the reference. */
export function citedBy(item: EvidenceItem, cited: Map<string, string[]>): string[] {
  const found = new Set<string>();
  for (const [reference, findings] of cited) {
    if (reference === item.record_id || reference.includes(item.record_id)) findings.forEach((f) => found.add(f));
  }
  return [...found];
}

function Quality({ item }: { item: EvidenceItem }) {
  const quality = item.data_quality.quality ?? {};
  const total = Object.values(quality).reduce((sum, n) => sum + n, 0);
  if (total === 0) return null;
  const good = quality.ok ?? 0;
  const share = Math.round((100 * good) / total);
  return (
    <div className="mt-2 space-y-1 text-xs" data-testid="data-quality">
      <div className="h-2 w-full overflow-hidden rounded bg-amber-100" aria-hidden="true">
        <div className="h-2 bg-emerald-500" style={{ width: `${share}%` }} />
      </div>
      <p className="break-words text-slate-600">
        {share}% of {total} readings checked as good
        {Object.entries(quality)
          .filter(([label]) => label !== "ok")
          .map(([label, n]) => ` · ${n} ${label.replaceAll("_", " ")}`)
          .join("")}
        {item.data_quality.gaps ? ` · ${item.data_quality.gaps} missing in gaps` : ""}
        {item.data_quality.rejected ? ` · ${item.data_quality.rejected} rejected` : ""}
      </p>
    </div>
  );
}

function Item({ item, cited }: { item: EvidenceItem; cited: Map<string, string[]> }) {
  const findings = citedBy(item, cited);
  return (
    <li className="rounded-md border border-slate-200 p-3" data-testid={`evidence-${item.record_type}`}>
      <div className="flex flex-col gap-1 sm:flex-row sm:items-start sm:justify-between">
        <span className="min-w-0 break-all font-mono text-xs text-slate-800">{item.record_id}</span>
        <span className="shrink-0 text-xs text-slate-500">version {item.version.length > 16 ? `${item.version.slice(0, 12)}…` : item.version}</span>
      </div>
      <dl className="mt-2 grid gap-x-3 gap-y-0.5 text-sm sm:grid-cols-[8rem_1fr]">
        {factsOf(item).map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-slate-500">{label}</dt>
            <dd className="min-w-0 break-words text-slate-800">{value}</dd>
          </div>
        ))}
      </dl>
      {item.record_type === "telemetry_snapshot" && <Quality item={item} />}
      <p className="mt-2 break-words text-xs text-slate-500">
        {findings.length ? `Cited by ${findings.join(", ")}` : "Not cited by a finding"}
      </p>
      <details className="mt-1 text-xs">
        <summary className="cursor-pointer text-brand-primary">The record as frozen</summary>
        <p className="mt-1 break-all text-slate-500">sha256 {item.content_hash}</p>
        <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-all rounded bg-slate-50 p-2 text-slate-700">
          {JSON.stringify(item.content, null, 2)}
        </pre>
      </details>
    </li>
  );
}

function Snapshot({ snapshot, cited }: { snapshot: EvidenceSnapshot; cited: Map<string, string[]> }) {
  const held = new Set(snapshot.items.map((item) => item.record_id));
  const elsewhere = [...cited].filter(([reference]) => !held.has(reference) && ![...held].some((id) => reference.includes(id)));
  return (
    <div className="space-y-4">
      <Panel title={`Snapshot version ${snapshot.version}`}>
        <div className="space-y-2 text-sm" data-testid="snapshot">
          <div className="flex flex-wrap items-center gap-2">
            {snapshot.verified ? (
              <StatusBadge status="Hashes match" className="bg-emerald-50 text-emerald-800 ring-1 ring-emerald-200" />
            ) : (
              <StatusBadge status="Hashes do not match" className="bg-red-50 text-red-800 ring-1 ring-red-200" />
            )}
            <span className="break-all font-mono text-xs text-slate-500">{snapshot.snapshot_id}</span>
          </div>
          <p className="break-words text-slate-600">
            {snapshot.item_count} items, from {when(snapshot.window_start)} to {when(snapshot.window_end)}. Frozen by{" "}
            {snapshot.created_by} {when(snapshot.created_at)}.
          </p>
          <p className="break-all text-xs text-slate-500">
            sha256 {snapshot.content_hash} ({snapshot.hash_format})
          </p>
          {!snapshot.verified && (
            <p role="alert" className="text-sm text-red-800">
              Something it holds no longer matches its hash. Do not rely on this snapshot.
            </p>
          )}
        </div>
      </Panel>
      {GROUPS.map(({ type, title }) => {
        const items = snapshot.items.filter((item) => item.record_type === type);
        if (items.length === 0) return null;
        return (
          <Panel key={type} title={`${title} (${items.length})`}>
            <ul className="space-y-2" aria-label={title}>
              {items.map((item) => (
                <Item key={`${item.record_type}-${item.record_id}`} item={item} cited={cited} />
              ))}
            </ul>
          </Panel>
        );
      })}
      {elsewhere.length > 0 && (
        <Panel title="Other citations">
          <p className="mb-2 text-xs text-slate-500">What the findings cite beyond this snapshot, as the agents' tools returned it.</p>
          <ul className="space-y-1 text-sm" aria-label="Other citations" data-testid="other-citations">
            {elsewhere.map(([reference, findings]) => (
              <li key={reference} className="break-words">
                <span className="text-slate-500">{sourceOf(reference)}:</span>{" "}
                <span className="break-all font-mono text-xs">{reference}</span>
                {findings.length > 0 && <span className="text-xs text-slate-500"> · cited by {findings.join(", ")}</span>}
              </li>
            ))}
          </ul>
        </Panel>
      )}
    </div>
  );
}

function Freeze({ api, caseId, onDone }: { api: InvestigationApi; caseId: string; onDone: (snapshotId: string) => void }) {
  const change = useChange();
  return (
    <div>
      <Button
        variant="outline"
        className="text-brand-primary"
        disabled={change.state.kind === "busy"}
        onClick={() => void change.run((key) => api.freeze(caseId, key)).then((made) => made && onDone(made.snapshot_id))}
      >
        {change.state.kind === "busy" ? "Freezing…" : "Freeze new evidence"}
      </Button>
      <ChangeStatus state={change.state} onDismiss={change.reset} />
    </div>
  );
}

export function EvidenceViewer({
  api,
  caseId,
  snapshotId,
  canFreeze,
}: {
  api: InvestigationApi;
  caseId: string;
  snapshotId: string | null;
  canFreeze: boolean;
}) {
  const loadCase = useCallback(() => api.detail(caseId), [api, caseId]);
  const [detail, reloadDetail] = useLoad(loadCase);
  const known = detail.kind === "ready" ? detail.data : null;
  const stale = known?.stale_rule ? `${known.stale_rule}: ${known.stale_detail}` : "";
  const active = known ? !["closed", "cancelled"].includes(known.investigation.status) : false;
  const [chosen, setChosen] = useState<string | null>(snapshotId);
  const loadList = useCallback(() => api.snapshots(caseId), [api, caseId]);
  const loadReport = useCallback(() => api.report(caseId), [api, caseId]);
  const [list, reloadList] = useLoad(loadList);
  const [report] = useLoad(loadReport);
  const newest = list.kind === "ready" ? list.data[0]?.snapshot_id : undefined;
  const shown = chosen ?? newest ?? null;
  const loadOne = useCallback(() => (shown ? api.snapshot(shown) : Promise.resolve(null)), [api, shown]);
  const [one] = useLoad(loadOne);
  const cited = citationsOf(report.kind === "ready" ? report.data.report : null);
  return (
    <section className="space-y-4">
      <Link to={`?case=${encodeURIComponent(caseId)}`} className="text-sm text-brand-primary hover:underline">
        ← The case
      </Link>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-xl font-semibold">Evidence</h2>
        {canFreeze && active && (
          <Freeze
            api={api}
            caseId={caseId}
            onDone={(made) => {
              setChosen(made);
              reloadList();
              reloadDetail();
            }}
          />
        )}
      </div>
      {stale && (
        <p role="note" className="break-words rounded-md border border-orange-200 bg-orange-50 p-3 text-sm text-orange-900">
          The newest snapshot is stale: {stale}. Freezing new evidence re-evaluates the case, and its work order is then revised
          on it as a new version.
        </p>
      )}
      <Loading state={list}>
        {(versions) =>
          versions.length === 0 ? (
            <p className="text-sm text-slate-500">No evidence frozen yet.</p>
          ) : (
            <div role="group" aria-label="Versions" className="flex flex-wrap gap-2">
              {versions.map((version) => (
                <button
                  key={version.snapshot_id}
                  type="button"
                  aria-pressed={version.snapshot_id === shown}
                  onClick={() => setChosen(version.snapshot_id)}
                  className={`rounded-full px-3 py-1 text-sm ring-1 ${
                    version.snapshot_id === shown ? "bg-brand-primary text-white ring-brand-primary" : "bg-white text-slate-700 ring-slate-300"
                  }`}
                >
                  Version {version.version}
                  {version.snapshot_id === newest ? " (newest)" : ""}
                </button>
              ))}
            </div>
          )
        }
      </Loading>
      <Loading state={one}>{(snapshot) => (snapshot ? <Snapshot snapshot={snapshot} cited={cited} /> : null)}</Loading>
      <p className="text-xs text-slate-500">
        Each item is kept as it was when frozen, with its own hash, and the snapshot's hash covers them all.
      </p>
    </section>
  );
}
