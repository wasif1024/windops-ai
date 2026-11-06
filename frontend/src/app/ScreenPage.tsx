import { useParams } from "react-router-dom";
import { screenById, type Screen } from "./navigation";

/** A screen the user's roles include. Until its feature is built, it says which phase builds it. */
export function ScreenPage({ screens }: { screens: Screen[] }) {
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
  return (
    <section>
      <h2 className="text-xl font-semibold">{screen.title}</h2>
      <p className="mt-2 text-slate-500">This screen arrives in {screen.arrives}.</p>
    </section>
  );
}
