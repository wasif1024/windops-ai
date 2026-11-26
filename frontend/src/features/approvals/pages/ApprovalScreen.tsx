import { useCallback, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useChange } from "../../../api/useChange";
import { useLoad } from "../../../api/useLoad";
import { Button } from "../../../components/Button";
import { ChangeStatus } from "../../../components/ChangeStatus";
import { INPUT, LABEL, Loading, Panel, when } from "../../../components/Panel";
import { StatusBadge } from "../../fleet/Freshness";
import { titleCase } from "../../fleet/format";
import { maintenanceApi, type MaintenanceApi, POLICY_STYLE } from "../../maintenance/api";
import { History, PlanView, ValidationView } from "../../maintenance/pages/WorkOrderPage";
import { type InvestigationApi, investigationApi } from "../../investigations/api";
import { Concerns } from "../../investigations/pages/Concerns";
import { type Approval, type ApprovalApi, approvalApi, APPROVAL_STYLE, type ApprovalStatus, draftedByOf } from "../api";

interface View {
  id: string;
  label: string;
  statuses: ApprovalStatus[];
}
const VIEWS: View[] = [
  { id: "pending", label: "Pending", statuses: ["pending"] },
  { id: "decided", label: "Decided", statuses: ["approved", "rejected"] },
  { id: "superseded", label: "Superseded", statuses: ["superseded"] },
  { id: "all", label: "All", statuses: [] },
];

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4 border-b border-slate-100 py-1.5 text-sm">
      <dt className="shrink-0 text-slate-500">{label}</dt>
      <dd className="min-w-0 break-words text-right font-medium text-slate-800">{value || "—"}</dd>
    </div>
  );
}

function Row({ approval }: { approval: Approval }) {
  return (
    <li>
      <Link
        to={`?approval=${encodeURIComponent(approval.approval_id)}`}
        className="flex flex-col gap-2 rounded-lg border border-slate-200 bg-white p-3 hover:border-brand-primary hover:shadow-sm sm:flex-row sm:items-center"
        data-testid={`approval-${approval.approval_id}`}
      >
        <span className="flex shrink-0 flex-wrap items-center gap-2 sm:w-48">
          <StatusBadge status={titleCase(approval.status)} className={APPROVAL_STYLE[approval.status]} />
          {approval.drafted_by_agent && <StatusBadge status="Agent's draft" className="bg-violet-50 text-violet-800 ring-1 ring-violet-200" />}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block break-all font-medium text-slate-900">
            {approval.work_order_id} · version {approval.proposal_version}
          </span>
          <span className="block break-words text-xs text-slate-500">
            {approval.turbine_id} · submitted by {approval.submitted_by}
          </span>
        </span>
        <span className="shrink-0 text-xs text-slate-500 sm:w-40 sm:text-right">{when(approval.requested_at)}</span>
      </Link>
    </li>
  );
}

/** Requests for a decision, oldest first, so they are worked in the order they arrived. */
export function ApprovalQueue({ api }: { api: ApprovalApi }) {
  const [view, setView] = useState(VIEWS[0] as View);
  const load = useCallback(() => api.list(view.statuses), [api, view]);
  const [state] = useLoad(load);
  return (
    <section className="space-y-4">
      <h2 className="text-xl font-semibold">Approval queue</h2>
      <Panel title="Requests">
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
            data.approvals.length === 0 ? (
              <p className="text-slate-500" data-testid="no-approvals">
                No {view.id === "all" ? "" : `${view.label.toLowerCase()} `}requests.
              </p>
            ) : (
              <ul className="space-y-2" aria-label="Requests">
                {data.approvals.map((approval) => (
                  <Row key={approval.approval_id} approval={approval} />
                ))}
              </ul>
            )
          }
        </Loading>
      </Panel>
    </section>
  );
}

/** Approve or reject, on exactly the version and snapshot shown. */
function DecideForm({ api, approval, onDone }: { api: ApprovalApi; approval: Approval; onDone: () => void }) {
  const change = useChange();
  const [reason, setReason] = useState("");
  const busy = change.state.kind === "busy";
  const decide = async (decision: "approve" | "reject") => {
    if (await change.run((key) => api.decide(approval.approval_id, key, approval, decision, reason.trim()))) onDone();
  };
  return (
    <div className="space-y-3">
      <p className="text-sm text-slate-600">
        You decide on version {approval.proposal_version}, on evidence snapshot{" "}
        <span className="break-all font-mono text-xs">{approval.evidence_snapshot_id}</span>. Both are checked again as you
        decide: if either has moved on, the decision is refused.
      </p>
      <label className={LABEL} htmlFor="decision-reason">
        Reason (needed to reject)
      </label>
      <textarea
        id="decision-reason"
        className={INPUT}
        rows={3}
        maxLength={1000}
        value={reason}
        onChange={(e) => setReason(e.target.value)}
      />
      <div className="flex flex-wrap gap-2">
        <Button disabled={busy} onClick={() => void decide("approve")}>
          Approve
        </Button>
        <Button variant="outline" className="text-red-700" disabled={busy || !reason.trim()} onClick={() => void decide("reject")}>
          Reject
        </Button>
      </div>
      <ChangeStatus state={change.state} onDismiss={change.reset} />
      {change.state.kind === "failed" && (
        <Button variant="outline" className="text-brand-primary" onClick={onDone}>
          Show it as it is now
        </Button>
      )}
    </div>
  );
}

function DecisionView({ approval }: { approval: Approval }) {
  const decision = approval.decision;
  if (!decision) return null;
  const policy = decision.policy;
  return (
    <div className="space-y-3" data-testid="decision">
      <dl>
        <Fact label="Decision" value={titleCase(decision.outcome)} />
        <Fact label="By" value={decision.decided_by} />
        <Fact label="When" value={when(decision.decided_at)} />
        <Fact label="Version" value={String(decision.proposal_version)} />
        <Fact label="Evidence snapshot" value={decision.evidence_snapshot_id} />
        <Fact label="Case was" value={titleCase(policy.case_status)} />
        <Fact label="Could not decide" value={decision.requesters.join(", ")} />
        <Fact label="Rules" value={`${policy.approval_rules}, permissions version ${policy.permissions_version}`} />
      </dl>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="text-slate-500">Policy</span>
        <StatusBadge status={titleCase(policy.policy_outcome)} className={POLICY_STYLE[policy.policy_outcome] ?? ""} />
        <span className="break-all font-mono text-xs text-slate-500">{policy.policy_result_id}</span>
      </div>
      {decision.reason && <p className="whitespace-pre-line break-words text-sm text-slate-700">“{decision.reason}”</p>}
    </div>
  );
}

function WorkOrderPanels({ maintenance, workOrderId }: { maintenance: MaintenanceApi; workOrderId: string }) {
  const load = useCallback(() => maintenance.get(workOrderId), [maintenance, workOrderId]);
  const [state] = useLoad(load);
  return (
    <Loading state={state}>
      {({ work_order: order, history }) => (
        <>
          <h3 className="break-words text-lg font-semibold">{order.plan.title || "Untitled"}</h3>
          <div className="grid gap-4 lg:grid-cols-2">
            <Panel title="Plan">
              <PlanView order={order} />
            </Panel>
            <div className="space-y-4">
              <Panel title="Validation">
                <ValidationView validation={order.validation} />
              </Panel>
              <Panel title="History">
                <History history={history} />
              </Panel>
            </div>
          </div>
        </>
      )}
    </Loading>
  );
}

/** One request: what is to be decided, and the decision, or the form to make it. */
export function ApprovalPage({
  api,
  maintenance,
  approvalId,
  canDecide,
  investigations,
}: {
  api: ApprovalApi;
  maintenance: MaintenanceApi;
  approvalId: string;
  canDecide: boolean;
  /** To show the findings reviewers questioned, beside the proposal (FR-INV-006). */
  investigations?: InvestigationApi;
}) {
  const load = useCallback(() => api.get(approvalId), [api, approvalId]);
  const [state, reload] = useLoad(load);
  return (
    <section className="space-y-4">
      <Link to="?" className="text-sm text-brand-primary hover:underline">
        ← Approval queue
      </Link>
      <Loading state={state}>
        {(approval) => (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="break-all text-xl font-semibold">
                {approval.work_order_id} · version {approval.proposal_version}
              </h2>
              <StatusBadge status={titleCase(approval.status)} className={APPROVAL_STYLE[approval.status]} />
            </div>
            <p className="break-words text-sm text-slate-500">
              Drafted by {draftedByOf(approval)} · submitted by {approval.submitted_by} {when(approval.submitted_at)} ·{" "}
              {approval.turbine_id}
            </p>
            {approval.drafted_by_agent && approval.status === "pending" && (
              <p role="note" className="rounded-md border border-violet-200 bg-violet-50 p-3 text-sm text-violet-900">
                An agent drafted this, and a person submitted it. Check the plan and its validation before you decide.
              </p>
            )}
            {approval.status === "superseded" && (
              <p role="note" className="break-words rounded-md border border-slate-200 bg-slate-50 p-3 text-sm text-slate-700">
                Superseded {when(approval.superseded_at)}: {approval.superseded_reason}. It can no longer be decided.
              </p>
            )}
            {investigations && approval.status === "pending" && <Concerns api={investigations} caseId={approval.case_id} />}
            <Panel title={approval.decision ? "Decision" : "Decide"}>
              {approval.decision ? (
                <DecisionView approval={approval} />
              ) : approval.status === "pending" && canDecide ? (
                <DecideForm api={api} approval={approval} onDone={reload} />
              ) : (
                <p className="text-sm text-slate-500">
                  {approval.status === "pending" ? "Waiting for a Maintenance Manager to decide." : "Nothing to decide."}
                </p>
              )}
            </Panel>
            <WorkOrderPanels maintenance={maintenance} workOrderId={approval.work_order_id} />
          </>
        )}
      </Loading>
    </section>
  );
}

/** The approval queue: the requests, or one when the address names it. */
export function ApprovalScreen({ token, held }: { token: string; held: ReadonlySet<string> }) {
  const api = useMemo(() => approvalApi(token), [token]);
  const maintenance = useMemo(() => maintenanceApi(token), [token]);
  const investigations = useMemo(() => investigationApi(token), [token]);
  const [params] = useSearchParams();
  const approval = params.get("approval");
  if (approval) {
    return (
      <ApprovalPage
        api={api}
        maintenance={maintenance}
        approvalId={approval}
        canDecide={held.has("maintenance.approve")}
        investigations={investigations}
      />
    );
  }
  return <ApprovalQueue api={api} />;
}
