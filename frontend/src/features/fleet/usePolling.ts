import { useEffect } from "react";

/** Call `reload` every `milliseconds` while the page is in view, so live screens stay current. */
export function usePolling(reload: () => void, milliseconds: number): void {
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") reload();
    }, milliseconds);
    return () => window.clearInterval(timer);
  }, [reload, milliseconds]);
}
