import { useAuth } from "../auth/auth";
import { Button } from "./Button";
import type { ChangeState } from "../api/useChange";

/** Verify with a second factor, then come back to this page, where the change can be made again. */
function StepUp({ onDismiss }: { onDismiss: () => void }) {
  const { signIn } = useAuth();
  return (
    <div role="alert" className="mt-3 rounded-md border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900">
      <p className="font-medium">This change needs your second factor.</p>
      <p className="mt-1">Verify with your authenticator app, then make the change again.</p>
      <div className="mt-3 flex flex-wrap gap-2">
        <Button
          onClick={() => void signIn({ acrValues: "mfa", returnTo: window.location.pathname + window.location.search })}
        >
          Verify now
        </Button>
        <Button variant="outline" className="text-amber-900" onClick={onDismiss}>
          Not now
        </Button>
      </div>
    </div>
  );
}

/** Shows what happened to a change, including a request for a recent second factor (SEC-003). */
export function ChangeStatus({ state, onDismiss }: { state: ChangeState; onDismiss: () => void }) {
  if (state.kind === "step-up") return <StepUp onDismiss={onDismiss} />;
  if (state.kind === "failed") {
    return (
      <p role="alert" className="mt-3 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
        {state.message}
      </p>
    );
  }
  return null;
}
