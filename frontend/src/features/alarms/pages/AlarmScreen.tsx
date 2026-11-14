import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { alarmsApi } from "../api";
import { AlarmConsole } from "./AlarmConsole";
import { AlarmPage } from "./AlarmPage";

/** The alarm console, or one alarm when the address names it, as ?alarm=. */
export function AlarmScreen({ token, held }: { token: string; held: ReadonlySet<string> }) {
  const api = useMemo(() => alarmsApi(token), [token]);
  const [params] = useSearchParams();
  const alarm = params.get("alarm");
  return alarm ? <AlarmPage api={api} alarmId={alarm} canAct={held.has("alarm.acknowledge")} /> : <AlarmConsole api={api} />;
}
