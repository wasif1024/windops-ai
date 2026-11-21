import { useCallback, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useChange } from "../../../api/useChange";
import { useLoad } from "../../../api/useLoad";
import { ChangeStatus } from "../../../components/ChangeStatus";
import { INPUT, LABEL, Loading, Panel, when } from "../../../components/Panel";
import { StatusBadge } from "../../fleet/Freshness";
import { titleCase } from "../../fleet/format";
import { type MaintenanceApi, STATUS_STYLE, type WorkOrder, type WorkOrderStatus } from "../api";
import { emptyPlan, toPlan } from "../form";
import { PlanEditor } from "./PlanEditor";

interface View {
  id: string;
  label: string;
  statuses: WorkOrderStatus[];
}
const VIEWS: View[] = [
  { id: "draft", label: "Drafts", statuses: ["draft"] },
  { id: "submitted", label: "Submitted", statuses: ["submitted"] },
  { id: "decided", label: "Decided", statuses: ["approved", "rejected"] },
  { id: "withdrawn", label: "Withdrawn", statuses: ["withdrawn"] },
  { id: "all", label: "All", statuses: [] },
];

function Row({ order }: { order: WorkOrder }) {
  const blocking = order.validation.problems.filter((p) => p.blocking).length;
  return (
    <li>
      <Link
        to={`?work_order=${encodeURIComponent(order.work_order_id)}`}
        className="flex flex-col gap-2 rounded-lg border border-slate-200 bg-white p-3 hover:border-brand-primary hover:shadow-sm sm:flex-row sm:items-center"
        data-testid={`work-order-${order.work_order_id}`}
      >
        <span className="flex shrink-0 flex-wrap items-center gap-2 sm:w-48">
          <StatusBadge status={titleCase(order.status)} className={STATUS_STYLE[order.status]} />
          {order.drafted_by_agent && <StatusBadge status="Agent's draft" className="bg-violet-50 text-violet-800 ring-1 ring-violet-200" />}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block break-words font-medium text-slate-900">{order.plan.title || "Untitled"}</span>
          <span className="block text-xs text-slate-500">
            {titleCase(order.plan.action)} · {order.turbine_id}
            {order.status === "draft" && (blocking ? ` · ${blocking} to fix` : " · ready to submit")}
          </span>
        </span>
        <span className="shrink-0 text-xs text-slate-500 sm:w-40 sm:text-right">{when(order.updated_at)}</span>
      </Link>
    </li>
  );
}

/** The planner's work orders, drafts first, each with whether it can be submitted. */
export function WorkOrderList({ api, canDraft }: { api: MaintenanceApi; canDraft: boolean }) {
  const [view, setView] = useState(VIEWS[0] as View);
  const load = useCallback(() => api.list(view.statuses), [api, view]);
  const [state] = useLoad(load);
  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-xl font-semibold">Maintenance planner</h2>
        {canDraft && (
          <Link to="?new=1" className="rounded-md bg-brand-primary px-4 py-2 font-medium text-white hover:opacity-90">
            New draft
          </Link>
        )}
      </div>
      <Panel title="Work orders">
        <div role="group" aria-label="Show" className="mb-4 flex flex-wrap gap-2">
          {VIEWS.map((option) => (
            <button
              key={option.id}
              type="button"
              aria-pressed={option.id === view.id}
              onClick={() => setView(option)}
              className={`rounded-full px-3 py-1 text-sm ring-1 ${
                option.id === view.id ? "bg-brand-primary text-white ring-brand-primary" : "bg-white text-slate-700 ring-slate-300"
              }`}
            >
              {option.label}
            </button>
          ))}
        </div>
        <Loading state={state}>
          {(data) =>
            data.work_orders.length === 0 ? (
              <p className="text-slate-500" data-testid="no-work-orders">
                No {view.id === "all" ? "" : `${view.label.toLowerCase()} `}work orders.
              </p>
            ) : (
              <ul className="space-y-2" aria-label="Work orders">
                {data.work_orders.map((order) => (
                  <Row key={order.work_order_id} order={order} />
                ))}
              </ul>
            )
          }
        </Loading>
      </Panel>
    </section>
  );
}

/** A new draft, for one of the active cases the planner may read. */
export function NewDraft({ api }: { api: MaintenanceApi }) {
  const navigate = useNavigate();
  const load = useCallback(() => api.cases(), [api]);
  const [cases] = useLoad(load);
  const [caseId, setCaseId] = useState("");
  const change = useChange();
  return (
    <section className="space-y-4">
      <Link to="?" className="text-sm text-brand-primary hover:underline">
        ← Maintenance planner
      </Link>
      <h2 className="text-xl font-semibold">New draft</h2>
      <Panel title="Plan">
        <Loading state={cases}>
          {(active) =>
            active.length === 0 ? (
              <p className="text-sm text-slate-500">There is no active case to plan work for.</p>
            ) : (
              <>
                <PlanEditor
                  initial={emptyPlan()}
                  busy={change.state.kind === "busy"}
                  submitLabel="Save draft"
                  onSave={(form) => {
                    const chosen = caseId || active[0]?.case_id || "";
                    void change.run((key) => api.draft(key, chosen, toPlan(form))).then((order) => {
                      if (order) navigate(`?work_order=${encodeURIComponent(order.work_order_id)}`);
                    });
                  }}
                  before={
                    <div className="space-y-1">
                      <label className={LABEL} htmlFor="plan-case">
                        Case
                      </label>
                      <select id="plan-case" className={INPUT} value={caseId || active[0]?.case_id} onChange={(e) => setCaseId(e.target.value)}>
                        {active.map((item) => (
                          <option key={item.case_id} value={item.case_id}>
                            {item.turbine_id}: {item.title} ({titleCase(item.severity)}, {titleCase(item.status)})
                          </option>
                        ))}
                      </select>
                    </div>
                  }
                />
                <ChangeStatus state={change.state} onDismiss={change.reset} />
              </>
            )
          }
        </Loading>
      </Panel>
    </section>
  );
}
