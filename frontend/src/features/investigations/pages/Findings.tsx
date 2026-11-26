// The investigation's findings (SRS 5.7), and each person's review of them (FR-INV-006). A review
// accepts, rejects, challenges or annotates one finding. It decides nothing: whoever decides on
// the case's work order sees it beside the proposal.
import { useState } from "react";
import { useChange } from "../../../api/useChange";
import { Button } from "../../../components/Button";
import { ChangeStatus } from "../../../components/ChangeStatus";
import { INPUT, when } from "../../../components/Panel";
import { StatusBadge } from "../../fleet/Freshness";
import { titleCase } from "../../fleet/format";
import {
  type FindingReview,
  type InvestigationApi,
  type Report,
  type ReportBody,
  REVIEW_STYLE,
  type ReviewAction,
  reviewsOf,
} from "../api";

interface Finding {
  name: string;
  text: string;
  detail?: string;
}

interface Section {
  id: keyof ReportBody;
  title: string;
  items: Finding[];
}

function percent(value: number | undefined): string {
  return value === undefined ? "" : `${Math.round(value * 100)}%`;
}

/** The report's reviewable lists, each finding named by where it is, as the service names it. */
export function sectionsOf(report: ReportBody): Section[] {
  const cite = (ids?: string[]) => (ids?.length ? `Cites ${ids.join(", ")}` : undefined);
  return [
    {
      id: "observed_facts",
      title: "Observed facts",
      items: (report.observed_facts ?? []).map((f, i) => ({
        name: `observed_facts[${i}]`,
        text: f.statement,
        detail: cite(f.citations),
      })),
    },
    {
      id: "hypotheses",
      title: "Hypotheses",
      items: (report.hypotheses ?? []).map((h, i) => ({
        name: `hypotheses[${i}]`,
        text: h.statement,
        detail: [
          h.confidence !== undefined && `Confidence ${percent(h.confidence)}`,
          h.support?.length && `supported by ${h.support.join(", ")}`,
          h.against?.length && `against: ${h.against.join(", ")}`,
        ]
          .filter(Boolean)
          .join(" · "),
      })),
    },
    {
      id: "conflicting_evidence",
      title: "Conflicting evidence",
      items: (report.conflicting_evidence ?? []).map((c, i) => ({
        name: `conflicting_evidence[${i}]`,
        text: c.description,
        detail: cite(c.citations),
      })),
    },
    {
      id: "data_gaps",
      title: "Data gaps",
      items: (report.data_gaps ?? []).map((text, i) => ({ name: `data_gaps[${i}]`, text })),
    },
    {
      id: "risk_flags",
      title: "Risk flags",
      items: (report.risk_flags ?? []).map((text, i) => ({ name: `risk_flags[${i}]`, text })),
    },
    {
      id: "recommended_actions",
      title: "Recommended actions",
      items: (report.recommended_actions ?? []).map((a, i) => ({
        name: `recommended_actions[${i}]`,
        text: [a.action && titleCase(a.action), a.summary].filter(Boolean).join(": "),
        detail: a.downtime_hours ? `About ${a.downtime_hours} hours of downtime` : undefined,
      })),
    },
  ].filter((section) => section.items.length > 0) as Section[];
}

const ACTIONS: { action: ReviewAction; label: string }[] = [
  { action: "accept", label: "Accept" },
  { action: "challenge", label: "Challenge" },
  { action: "reject", label: "Reject" },
  { action: "annotate", label: "Annotate" },
];

function ReviewForm({ api, caseId, finding, onDone }: { api: InvestigationApi; caseId: string; finding: string; onDone: () => void }) {
  const change = useChange();
  const [note, setNote] = useState("");
  const busy = change.state.kind === "busy";
  const send = async (action: ReviewAction) => {
    if (await change.run((key) => api.review(caseId, key, finding, action, note.trim()))) {
      setNote("");
      onDone();
    }
  };
  return (
    <div className="mt-2 space-y-2">
      <label className="sr-only" htmlFor={`note-${finding}`}>
        Note on {finding}
      </label>
      <textarea
        id={`note-${finding}`}
        className={INPUT}
        rows={2}
        maxLength={1000}
        placeholder="Why (needed to challenge, reject or annotate)"
        value={note}
        onChange={(e) => setNote(e.target.value)}
      />
      <div className="flex flex-wrap gap-2">
        {ACTIONS.map(({ action, label }) => (
          <Button
            key={action}
            variant={action === "accept" ? "primary" : "outline"}
            className={`px-3 py-1 text-sm ${action === "reject" ? "text-red-700" : action === "accept" ? "" : "text-slate-700"}`}
            disabled={busy || (action !== "accept" && !note.trim())}
            onClick={() => void send(action)}
          >
            {label}
          </Button>
        ))}
      </div>
      <ChangeStatus state={change.state} onDismiss={change.reset} />
    </div>
  );
}

function Reviews({ reviews }: { reviews: FindingReview[] }) {
  if (reviews.length === 0) return null;
  return (
    <ul className="mt-2 space-y-1 border-l-2 border-slate-200 pl-3 text-xs" aria-label="Reviews">
      {reviews.map((review) => (
        <li key={review.review_id} className="break-words text-slate-600">
          <StatusBadge status={titleCase(review.action)} className={REVIEW_STYLE[review.action]} /> {review.reviewed_by},{" "}
          {when(review.reviewed_at)}
          {review.note && <span className="block text-slate-800">“{review.note}”</span>}
        </li>
      ))}
    </ul>
  );
}

function FindingRow({
  api,
  caseId,
  finding,
  reviews,
  canReview,
  onReviewed,
}: {
  api: InvestigationApi;
  caseId: string;
  finding: Finding;
  reviews: FindingReview[];
  canReview: boolean;
  onReviewed: () => void;
}) {
  const [open, setOpen] = useState(false);
  const { all, latest } = reviewsOf(reviews, finding.name);
  return (
    <li className="rounded-md border border-slate-200 p-3" data-testid={`finding-${finding.name}`}>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <p className="break-words text-sm text-slate-900">{finding.text}</p>
          {finding.detail && <p className="mt-1 break-words text-xs text-slate-500">{finding.detail}</p>}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {latest && <StatusBadge status={titleCase(latest.action)} className={REVIEW_STYLE[latest.action]} />}
          {canReview && (
            <button
              type="button"
              className="text-sm text-brand-primary hover:underline"
              aria-expanded={open}
              onClick={() => setOpen((value) => !value)}
            >
              {open ? "Close" : "Review"}
            </button>
          )}
        </div>
      </div>
      <Reviews reviews={all} />
      {open && (
        <ReviewForm
          api={api}
          caseId={caseId}
          finding={finding.name}
          onDone={() => {
            setOpen(false);
            onReviewed();
          }}
        />
      )}
    </li>
  );
}

export function Findings({
  api,
  caseId,
  report,
  canReview,
  onReviewed,
}: {
  api: InvestigationApi;
  caseId: string;
  report: Report;
  canReview: boolean;
  onReviewed: () => void;
}) {
  if (!report.report) {
    return <p className="text-sm text-slate-500">The findings appear once the investigation completes.</p>;
  }
  const body = report.report;
  return (
    <div className="space-y-4" data-testid="findings">
      {body.explanation && <p className="whitespace-pre-line break-words text-sm text-slate-800">{body.explanation}</p>}
      <div className="flex flex-wrap gap-2 text-sm">
        {body.confidence !== undefined && (
          <StatusBadge status={`Confidence ${percent(body.confidence)}`} className="bg-slate-100 text-slate-700 ring-1 ring-slate-200" />
        )}
        {body.escalation_required && (
          <StatusBadge status="Escalation required" className="bg-red-50 text-red-800 ring-1 ring-red-200" />
        )}
        <span className="text-xs text-slate-500">
          Generated by agents, as hypotheses and cited facts: check each against its evidence.
        </span>
      </div>
      {sectionsOf(body).map((section) => (
        <div key={section.id}>
          <h4 className="mb-2 text-sm font-semibold text-slate-700">{section.title}</h4>
          <ul className="space-y-2" aria-label={section.title}>
            {section.items.map((finding) => (
              <FindingRow
                key={finding.name}
                api={api}
                caseId={caseId}
                finding={finding}
                reviews={report.reviews}
                canReview={canReview}
                onReviewed={onReviewed}
              />
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}
