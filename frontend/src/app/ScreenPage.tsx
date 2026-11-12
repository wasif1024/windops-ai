import { useParams } from "react-router-dom";
import { AdminPage } from "../features/admin/pages/AdminPage";
import { FleetScreen, TelemetryScreen } from "../features/fleet/pages/FleetScreen";
import { PlatformPage } from "../features/platform/pages/PlatformPage";
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
  return (
    <section>
      <h2 className="text-xl font-semibold">{screen.title}</h2>
      <p className="mt-2 text-slate-500">This screen arrives in {screen.arrives}.</p>
    </section>
  );
}
