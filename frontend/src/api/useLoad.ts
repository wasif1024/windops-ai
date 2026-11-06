import { useCallback, useEffect, useState } from "react";
import { messageFor } from "./useChange";

export type Loaded<T> = { kind: "loading" } | { kind: "failed"; message: string } | { kind: "ready"; data: T };

/**
 * Load when `load` changes, and again whenever `reload` is called, ignoring an answer that arrives
 * too late. Pass a `load` from useCallback, so it changes only when what it reads changes.
 */
export function useLoad<T>(load: () => Promise<T>): [Loaded<T>, () => void] {
  const [state, setState] = useState<Loaded<T>>({ kind: "loading" });
  const [round, setRound] = useState(0);

  useEffect(() => {
    let current = true;
    load()
      .then((data) => {
        if (current) setState({ kind: "ready", data });
      })
      .catch((error: unknown) => {
        if (current) setState({ kind: "failed", message: messageFor(error) });
      });
    return () => {
      current = false;
    };
  }, [load, round]);

  const reload = useCallback(() => setRound((value) => value + 1), []);
  return [state, reload];
}
