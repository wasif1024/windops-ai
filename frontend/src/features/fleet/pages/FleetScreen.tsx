import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { fleetApi } from "../api";
import { FleetPage } from "./FleetPage";
import { TelemetryPage } from "./TelemetryPage";
import { TurbinePage } from "./TurbinePage";

/** The fleet overview, or one turbine when the address names it, as ?turbine=. */
export function FleetScreen({ token }: { token: string }) {
  const api = useMemo(() => fleetApi(token), [token]);
  const [params] = useSearchParams();
  const turbine = params.get("turbine");
  return turbine ? <TurbinePage api={api} turbineId={turbine} back="?" /> : <FleetPage api={api} />;
}

export function TelemetryScreen({ token }: { token: string }) {
  const api = useMemo(() => fleetApi(token), [token]);
  return <TelemetryPage api={api} />;
}
