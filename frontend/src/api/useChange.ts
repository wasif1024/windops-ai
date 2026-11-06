// Run one change against Web BFF: keep its idempotency key until it succeeds, and notice when the
// service wants a recent second factor, so the screen can ask for one (RFC 9470).
import { useCallback, useRef, useState } from "react";
import { ApiError, newIdempotencyKey } from "./client";

export type ChangeState = { kind: "idle" } | { kind: "busy" } | { kind: "step-up" } | { kind: "failed"; message: string };

/** Our own words for each problem code, so nothing a service says reaches the screen. */
export function messageFor(error: unknown): string {
  if (!(error instanceof ApiError)) return "The request did not reach WindOps. Check your connection and try again.";
  switch (error.problem.code) {
    case "forbidden":
      return "You do not have permission to do this.";
    case "not_found":
      return "That no longer exists. Reload the page to see the latest.";
    case "conflict":
      return "This conflicts with what is already there, such as an email that is already used.";
    case "invalid_request":
      return "Some details are not valid. Check them and try again.";
    case "too_many_requests":
      return "Too many requests. Wait a minute and try again.";
    case "unavailable":
    case "bad_gateway":
      return "WindOps cannot do this right now. Try again in a moment.";
    default:
      return "Something went wrong. Try again.";
  }
}

/** Whether sending the same change again might succeed, so it keeps the same key. */
function mayResend(error: unknown): boolean {
  if (!(error instanceof ApiError)) return true;
  return ["unavailable", "bad_gateway", "too_many_requests", "step_up_required"].includes(error.problem.code ?? "");
}

export function useChange() {
  const [state, setState] = useState<ChangeState>({ kind: "idle" });
  const key = useRef<string | null>(null);

  const run = useCallback(async <T>(change: (key: string) => Promise<T>): Promise<T | undefined> => {
    key.current ??= newIdempotencyKey();
    setState({ kind: "busy" });
    try {
      const result = await change(key.current);
      key.current = null;
      setState({ kind: "idle" });
      return result;
    } catch (error) {
      if (!mayResend(error)) key.current = null;
      if (error instanceof ApiError && error.needsStepUp) setState({ kind: "step-up" });
      else setState({ kind: "failed", message: messageFor(error) });
      return undefined;
    }
  }, []);

  const reset = useCallback(() => setState({ kind: "idle" }), []);
  return { state, run, reset };
}
