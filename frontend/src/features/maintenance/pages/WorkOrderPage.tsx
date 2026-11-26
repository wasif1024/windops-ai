import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { useChange } from "../../../api/useChange";
import { useLoad } from "../../../api/useLoad";
import { Button } from "../../../components/Button";
import { ChangeStatus } from "../../../components/ChangeStatus";
import { INPUT, LABEL, Loading, Panel, when } from "../../../components/Panel";
import { StatusBadge } from "../../fleet/Freshness";
import { titleCase } from "../../fleet/format";
import {
  draftedBy,
  type MaintenanceApi,
  PART_STYLE,
  POLICY_STYLE,
  STATUS_STYLE,
  type Validation,
  type WorkOrder,
  type WorkOrderEvent,
} from "../api";
import { fromPlan, toPlan } from "../form";
import { CompleteWork, CompletionView } from "./Completion";
import { PlanEditor } from "./PlanEditor";

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4 border-b border-slate-100 py-1.5 text-sm">
      <dt className="shrink-0 text-slate-500">{label}</dt>
      <dd className="min-w-0 break-words text-right font-medium text-slate-800">{value || "—"}</dd>
    </div>
  );
}

export function PlanView({ order }: { order: WorkOrder }) {
  const plan = order.plan;
  const availability = new Map(order.validation.parts.map((p) => [p.part_number, p]));
  return (
    <div className="space-y-4">
      <dl>
        <Fact label="Action" value={titleCase(plan.action)} />
        <Fact label="Turbine" value={order.turbine_id} />
        <Fact label="Case" value={order.case_id} />
        <Fact label="Window" value={plan.planned_start ? `${when(plan.planned_start)} to ${when(plan.planned_end)}` : ""} />
        <Fact label="Expected downtime" value={`${plan.estimated_downtime_hours} h`} />
        <Fact label="Impact estimate" value={plan.impact_result_id} />
      </dl>
      {plan.summary && <p className="whitespace-pre-line break-words text-sm text-slate-700">{plan.summary}</p>}
      <div>
        <h4 className="text-sm font-semibold text-slate-700">Tasks</h4>
        <ol className="mt-1 space-y-1 text-sm" aria-label="Tasks">
          {plan.tasks.map((task, index) => (
            <li key={index} className="break-words">
              {index + 1}. {task.description}{" "}
              <span className="text-slate-500">
                · {task.duration_hours} h · crew of {task.crew_size}
                {task.requires_climb ? " · climb" : ""}
              </span>
            </li>
          ))}
        </ol>
      </div>
      <div>
        <h4 className="text-sm font-semibold text-slate-700">Parts</h4>
        {plan.parts.length === 0 ? (
          <p className="text-sm text-slate-500">None.</p>
        ) : (
          <ul className="mt-1 space-y-1 text-sm" aria-label="Parts">
            {plan.parts.map((part) => {
              const found = availability.get(part.part_number);
              return (
                <li key={part.part_number} className="flex flex-wrap items-center gap-2">
                  <span className="break-all font-mono">{part.part_number}</span>
                  <span className="text-slate-500">× {part.quantity}</span>
                  {found && (
                    <StatusBadge
                      status={found.status === "short" ? `Short: ${found.available} in the store` : titleCase(found.status)}
                      className={PART_STYLE[found.status]}
                    />
                  )}
                </li>
              );
            })}
          </ul>
        )}
        <p className="mt-1 text-xs text-slate-500">Naming a part reserves nothing.</p>
      </div>
      {plan.procedures.length > 0 && (
        <div>
          <h4 className="text-sm font-semibold text-slate-700">Procedures</h4>
          <ul className="mt-1 space-y-1 break-all font-mono text-sm">
            {plan.procedures.map((citation) => (
              <li key={citation}>{citation}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export function ValidationView({ validation }: { validation: Validation }) {
  const blocking = validation.problems.filter((p) => p.blocking);
  const noted = validation.problems.filter((p) => !p.blocking);
  return (
    <div className="space-y-3 text-sm" data-testid="validation">
      <p className={`font-medium ${validation.passed ? "text-emerald-800" : "text-red-800"}`}>
        {validation.passed ? "Nothing blocks it." : `${blocking.length} ${blocking.length === 1 ? "problem blocks" : "problems block"} it.`}
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-slate-500">Policy</span>
        {validation.policy_outcome ? (
          <StatusBadge status={titleCase(validation.policy_outcome)} className={POLICY_STYLE[validation.policy_outcome] ?? ""} />
        ) : (
          <span>not checked</span>
        )}
        <span className="text-slate-500">· Turbine</span>
        <span>{validation.turbine_status ? titleCase(validation.turbine_status) : "unknown"}</span>
      </div>
      {blocking.length > 0 && (
        <ul className="space-y-1" aria-label="Blocking problems">
          {blocking.map((p, index) => (
            <li key={index} className="break-words rounded border border-red-200 bg-red-50 px-2 py-1 text-red-900">
              <span className="font-mono text-xs">{p.field}</span> {p.message}
            </li>
          ))}
        </ul>
      )}
      {noted.length > 0 && (
        <ul className="space-y-1" aria-label="For the reviewer">
          {noted.map((p, index) => (
            <li key={index} className="break-words rounded border border-amber-200 bg-amber-50 px-2 py-1 text-amber-900">
              <span className="font-mono text-xs">{p.field}</span> {p.message}
            </li>
          ))}
        </ul>
      )}
      <p className="text-xs text-slate-500">Checked {when(validation.checked_at)}</p>
    </div>
  );
}

function who(event: WorkOrderEvent): string {
  if (event.agent) return `${event.agent.replace(/^agent:/, "").replaceAll("-", " ")} agent, for ${event.actor_id}`;
  return event.actor_id;
}

export function History({ history }: { history: WorkOrderEvent[] }) {
  return (
    <ol className="space-y-3" aria-label="History">
      {history.map((event, index) => (
        <li key={`${event.at}-${index}`} className="border-l-2 border-slate-200 pl-3 text-sm">
          <p className="font-medium text-slate-800">
            {titleCase(event.action)} <span className="font-normal text-slate-500">· revision {event.revision}</span>
          </p>
          <p className="break-words text-xs text-slate-500">
            {when(event.at)} · {who(event)}
          </p>
          {event.note && <p className="mt-1 break-words text-slate-700">“{event.note}”</p>}
        </li>
      ))}
    </ol>
  );
}

/** What the planner may do to a draft: change it, check it again, submit or withdraw it. */
function Actions({
  api,
  order,
  canSubmit,
  onEdit,
  onDone,
}: {
  api: MaintenanceApi;
  order: WorkOrder;
  canSubmit: boolean;
  onEdit: () => void;
  onDone: () => void;
}) {
  const change = useChange();
  const [reason, setReason] = useState("");
  const busy = change.state.kind === "busy";
  const run = async (action: (key: string) => Promise<WorkOrder>) => {
    if (await change.run(action)) onDone();
  };
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2">
        <Button onClick={onEdit} disabled={busy}>
          Edit plan
        </Button>
        <Button variant="outline" className="text-brand-primary" disabled={busy} onClick={() => void run((key) => api.validate(order.work_order_id, key))}>
          Check again
        </Button>
        {canSubmit && (
          <Button
            variant="outline"
            className="text-brand-primary"
            disabled={busy || !order.validation.passed}
            onClick={() => void run((key) => api.submit(order.work_order_id, key, order.revision))}
          >
            Submit for a decision
          </Button>
        )}
      </div>
      {canSubmit && !order.validation.passed && <p className="text-sm text-slate-500">Fix what blocks it, then submit it.</p>}
      <form
        className="space-y-2"
        onSubmit={(event) => {
          event.preventDefault();
          void run((key) => api.withdraw(order.work_order_id, key, reason.trim()));
        }}
      >
        <label className={LABEL} htmlFor="withdraw-reason">
          Reason to withdraw
        </label>
        <input id="withdraw-reason" className={INPUT} value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} />
        <Button type="submit" variant="outline" className="text-red-700" disabled={busy || !reason.trim()}>
          Withdraw
        </Button>
      </form>
      <ChangeStatus state={change.state} onDismiss={change.reset} />
    </div>
  );
}

/** A stale or rejected work order becomes its next version, a draft on fresh evidence. */
function Revise({ api, order, onDone }: { api: MaintenanceApi; order: WorkOrder; onDone: () => void }) {
  const change = useChange();
  return (
    <div>
      <Button
        disabled={change.state.kind === "busy"}
        onClick={() => void change.run((key) => api.revise(order.work_order_id, key, order.revision)).then((done) => done && onDone())}
      >
        Revise as version {order.version + 1}
      </Button>
      <ChangeStatus state={change.state} onDismiss={change.reset} />
    </div>
  );
}

/** One work order: its plan, what its validation found, every change, and what to do now. */
export function WorkOrderPage({
  api,
  workOrderId,
  canDraft,
  canSubmit,
  canComplete = false,
}: {
  api: MaintenanceApi;
  workOrderId: string;
  canDraft: boolean;
  canSubmit: boolean;
  canComplete?: boolean;
}) {
  const load = useCallback(() => api.get(workOrderId), [api, workOrderId]);
  const [state, reload] = useLoad(load);
  const [editing, setEditing] = useState(false);
  const save = useChange();
  return (
    <section className="space-y-4">
      <Link to="?" className="text-sm text-brand-primary hover:underline">
        ← Maintenance planner
      </Link>
      <Loading state={state}>
        {({ work_order: order, history, earlier_versions }) => (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="break-words text-xl font-semibold">{order.plan.title || "Untitled"}</h2>
              <StatusBadge status={titleCase(order.status)} className={STATUS_STYLE[order.status]} />
            </div>
            <p className="break-words text-sm text-slate-500">
              Drafted by {draftedBy(order)} · revision {order.revision}
              {order.submitted_by && ` · submitted by ${order.submitted_by} ${when(order.submitted_at)}`}
            </p>
            {order.status === "stale" && (
              <div role="note" className="rounded-md border border-orange-200 bg-orange-50 p-3 text-sm text-orange-900" data-testid="stale">
                <p className="break-words">
                  Its evidence is stale ({order.stale_rule}): {order.stale_detail}. It cannot be approved. Once the case is
                  re-evaluated, revise it as version {order.version + 1}.
                </p>
              </div>
            )}
            {(order.status === "stale" || order.status === "rejected") && canDraft && (
              <Revise api={api} order={order} onDone={reload} />
            )}
            {order.drafted_by_agent && order.status === "draft" && (
              <p role="note" className="rounded-md border border-violet-200 bg-violet-50 p-3 text-sm text-violet-900">
                An agent drafted this. Check every task, part and procedure before you submit it: only a person submits, and
                another decides.
              </p>
            )}
            {order.status === "approved" && canComplete && (
              <Panel title="Record the work done">
                <CompleteWork api={api} order={order} onDone={reload} />
              </Panel>
            )}
            {order.completion && (
              <Panel title="Work done">
                <CompletionView completion={order.completion} outcome={order.verification_outcome} />
              </Panel>
            )}
            {editing ? (
              <Panel title="Edit plan">
                <PlanEditor
                  initial={fromPlan(order.plan)}
                  busy={save.state.kind === "busy"}
                  submitLabel="Save changes"
                  onCancel={() => setEditing(false)}
                  onSave={(form) => {
                    void save.run((key) => api.update(order.work_order_id, key, order.revision, toPlan(form))).then((saved) => {
                      if (saved) {
                        setEditing(false);
                        reload();
                      }
                    });
                  }}
                />
                <ChangeStatus state={save.state} onDismiss={save.reset} />
              </Panel>
            ) : (
              <div className="grid gap-4 lg:grid-cols-2">
                <Panel title="Plan">
                  <PlanView order={order} />
                </Panel>
                <div className="space-y-4">
                  <Panel title="Validation">
                    <ValidationView validation={order.validation} />
                  </Panel>
                  {order.status === "draft" && canDraft && (
                    <Panel title="What to do">
                      <Actions api={api} order={order} canSubmit={canSubmit} onEdit={() => setEditing(true)} onDone={reload} />
                    </Panel>
                  )}
                </div>
              </div>
            )}
            <Panel title="History">
              <History history={history} />
            </Panel>
            {earlier_versions.length > 0 && (
              <Panel title="Earlier versions">
                <ul className="space-y-1 text-sm" aria-label="Earlier versions">
                  {earlier_versions.map((earlier) => (
                    <li key={earlier.version} className="break-words">
                      Version {earlier.version}: {titleCase(earlier.status)}
                      {earlier.stale_rule && ` (${earlier.stale_rule})`} · snapshot{" "}
                      <span className="font-mono text-xs">{earlier.evidence_snapshot_id}</span>
                    </li>
                  ))}
                </ul>
              </Panel>
            )}
          </>
        )}
      </Loading>
    </section>
  );
}
