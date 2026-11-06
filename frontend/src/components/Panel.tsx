import { type ReactNode, useId } from "react";
import type { Loaded } from "../api/useLoad";

/** A titled white card, the console's one container, named by its title for screen readers. */
export function Panel({ title, children, actions }: { title: string; children: ReactNode; actions?: ReactNode }) {
  const heading = useId();
  return (
    <section aria-labelledby={heading} className="rounded-lg border border-slate-200 bg-white p-4 md:p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id={heading} className="break-all text-base font-semibold">
          {title}
        </h3>
        {actions}
      </div>
      <div className="mt-3">{children}</div>
    </section>
  );
}

/** What a list shows while it loads, if it fails, or once it has data. */
export function Loading<T>({ state, children }: { state: Loaded<T>; children: (data: T) => ReactNode }) {
  if (state.kind === "loading") return <p className="text-sm text-slate-500">Loading…</p>;
  if (state.kind === "failed") {
    return (
      <p role="alert" className="text-sm text-red-800">
        {state.message}
      </p>
    );
  }
  return <>{children(state.data)}</>;
}

export function when(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}

export const INPUT = "w-full rounded-md border border-slate-300 px-3 py-2 text-sm";
export const LABEL = "block text-sm font-medium text-slate-700";
