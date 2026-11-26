import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { maintenanceApi } from "../api";
import { NewDraft, WorkOrderList } from "./WorkOrderList";
import { WorkOrderPage } from "./WorkOrderPage";

/** The maintenance planner: the work orders, one when the address names it, or a new draft. */
export function MaintenanceScreen({ token, held }: { token: string; held: ReadonlySet<string> }) {
  const api = useMemo(() => maintenanceApi(token), [token]);
  const [params] = useSearchParams();
  const canDraft = held.has("maintenance.draft");
  const workOrder = params.get("work_order");
  if (workOrder) {
    return (
      <WorkOrderPage
        api={api}
        workOrderId={workOrder}
        canDraft={canDraft}
        canSubmit={held.has("maintenance.submit")}
        canComplete={held.has("maintenance.complete")}
      />
    );
  }
  if (params.get("new") && canDraft) return <NewDraft api={api} />;
  return <WorkOrderList api={api} canDraft={canDraft} />;
}
