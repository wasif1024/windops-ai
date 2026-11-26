// What a Maintenance Manager must know before deciding: the findings a reviewer challenged or
// rejected, and has not accepted since (FR-INV-006). They block nothing; the person decides.
import { useCallback } from "react";
import { Link } from "react-router-dom";
import { useLoad } from "../../../api/useLoad";
import { when } from "../../../components/Panel";
import { type InvestigationApi, openConcerns } from "../api";

export function Concerns({ api, caseId }: { api: InvestigationApi; caseId: string }) {
  const load = useCallback(() => api.report(caseId), [api, caseId]);
  const [state] = useLoad(load);
  if (state.kind !== "ready") return null;
  const concerns = openConcerns(state.data.reviews);
  if (concerns.length === 0) return null;
  return (
    <div role="note" className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900" data-testid="concerns">
      <p className="font-medium">
        Reviewers questioned {concerns.length === 1 ? "a finding" : `${concerns.length} findings`} this proposal rests on:
      </p>
      <ul className="mt-2 space-y-1">
        {concerns.map((review) => (
          <li key={review.review_id} className="break-words">
            {review.finding}: {review.action === "reject" ? "rejected" : "challenged"} by {review.reviewed_by}{" "}
            {when(review.reviewed_at)}, “{review.note}”
          </li>
        ))}
      </ul>
      <Link to={`/screens/investigations?case=${encodeURIComponent(caseId)}`} className="mt-2 inline-block text-brand-primary hover:underline">
        Open the case's findings
      </Link>
    </div>
  );
}
