import type { Latest } from "./api";
import { ageText, freshness, FRESHNESS_STYLE } from "./format";

/** How fresh a turbine's newest sample is: a coloured dot, a word, and its age. */
export function FreshnessBadge({ latest, now }: { latest: Latest | null; now: number }) {
  const state = freshness(latest, now);
  const style = FRESHNESS_STYLE[state];
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-slate-600" data-freshness={state}>
      <span aria-hidden="true" className={`h-2 w-2 rounded-full ${style.dot}`} />
      <span className="font-medium text-slate-700">{style.label}</span>
      <span>· {ageText(latest, now)}</span>
    </span>
  );
}

export function StatusBadge({ status, className }: { status: string; className: string }) {
  return <span className={`inline-block rounded px-2 py-0.5 text-xs font-medium ${className}`}>{status}</span>;
}
