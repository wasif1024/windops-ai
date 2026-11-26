import { useParams } from "react-router-dom";
import { AdminPage } from "../features/admin/pages/AdminPage";
import { AlarmScreen } from "../features/alarms/pages/AlarmScreen";
import { AuditScreen } from "../features/audit/pages/AuditScreen";
import { ApprovalScreen } from "../features/approvals/pages/ApprovalScreen";
import { FleetScreen, TelemetryScreen } from "../features/fleet/pages/FleetScreen";
import { ImpactScreen } from "../features/impact/pages/ImpactScreen";
import { KnowledgeScreen } from "../features/knowledge/pages/KnowledgeScreen";
import { InvestigationScreen } from "../features/investigations/pages/InvestigationScreen";
import { NotificationScreen } from "../features/notifications/pages/NotificationScreen";
import { MaintenanceScreen } from "../features/maintenance/pages/MaintenanceScreen";
import { PartsScreen } from "../features/parts/pages/PartsScreen";
import { PlatformPage } from "../features/platform/pages/PlatformPage";
import { WeatherScreen } from "../features/weather/pages/WeatherScreen";
import { screenById, type Screen } from "./navigation";

/** A screen the user's roles include. Until its feature is built, it says which phase builds it. */
export function ScreenPage({ screens, token, held }: { screens: Screen[]; token: string; held: ReadonlySet<string> }) {
  const { id } = useParams();
  const screen = screenById(id);
  if (!screen || !screens.includes(screen)) {
    return (
      <section>
        <h2 className="text-xl font-semibold">Not available</h2>
        <p className="mt-2 text-slate-600">This screen does not exist, or your roles do not include it.</p>
      </section>
    );
  }
  if (screen.id === "admin") return <AdminPage token={token} held={held} />;
  if (screen.id === "platform") return <PlatformPage token={token} />;
  if (screen.id === "fleet") return <FleetScreen token={token} />;
  if (screen.id === "telemetry") return <TelemetryScreen token={token} />;
  if (screen.id === "alarms") return <AlarmScreen token={token} held={held} />;
  if (screen.id === "maintenance") return <MaintenanceScreen token={token} held={held} />;
  if (screen.id === "parts") return <PartsScreen token={token} />;
  if (screen.id === "approvals") return <ApprovalScreen token={token} held={held} />;
  if (screen.id === "investigations") return <InvestigationScreen token={token} held={held} />;
  if (screen.id === "impact") return <ImpactScreen token={token} />;
  if (screen.id === "weather") return <WeatherScreen token={token} />;
  if (screen.id === "audit") return <AuditScreen token={token} />;
  if (screen.id === "notifications") return <NotificationScreen token={token} held={held} />;
  if (screen.id === "knowledge") return <KnowledgeScreen token={token} held={held} />;
  return (
    <section>
      <h2 className="text-xl font-semibold">{screen.title}</h2>
      <p className="mt-2 text-slate-500">This screen arrives in {screen.arrives}.</p>
    </section>
  );
}
