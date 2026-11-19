# ADR 0022: The structured result, checked against what the team saw

**Status:** Accepted, 2026-11-19, for M8.8.

## Context

SRS 5.7 sets the investigation result's fields:
- `case_id`, `asset_id`, `snapshot_id`;
- `observed_facts`, `hypotheses`, `evidence_references`, `conflicting_evidence`, `data_gaps`, `risk_flags`;
- `recommended_actions`, `confidence`, `explanation`, `escalation_required`;
- `policy_result_id`, `model_versions`, `generated_at`.

What the result must do:
- **FR-INV-003:** keep facts, hypotheses, unknowns, confidence and next steps apart.
- **FR-INV-004:** cite an evidence ID or external source for every material claim.
- **FR-INV-005:** flag contradictions and missing information, rather than invent a resolution.
- **FR-IMP-004:** the Production Impact agent explains figures, and never changes them.
- **SRS 5.4 step 10:** validate with at most two repairs.
- **Step 11:** run the deterministic checks through the Policy Service.

[ADR 0021](0021-the-agents-and-skills-on-deep-agents.md) left the Supervisor's report as text.

## Decision

**Step 10 turns the report into a `Draft`, in SRS 5.7's shape.**
- **How:** one call in the Supervisor's role, through Claude's structured outputs (LLM-004).
- **Its own prompt:** `agents/supervisor/result.md`, a second stable prompt, pinned with the others (AGT-004).
- **What it is given,** after the cached prompt: the case and snapshot IDs, every reference the team's tools
  returned, every impact estimate, and the report.

**The draft is then checked, deterministically, against what the tools actually returned in the run.**
- **Recording:** the team's tool calls record what each answer can be cited by: evidence record IDs, document
  citations such as `kn:DOC@3#12`, research sources, and case, estimate and policy result IDs. They also record
  every estimate's figures.
- **The checks:**

| Check | Requirement |
| --- | --- |
| The case and snapshot are the run's own | |
| Every observed fact cites at least one reference, and every reference was returned by a tool | FR-INV-003, FR-INV-004 |
| Every hypothesis has support, and a confidence from 0 to 1; what it cites was returned | FR-INV-003, FR-INV-004 |
| A conflict names at least two sources | FR-INV-005 |
| No facts at all means data gaps say why | FR-INV-005 |
| An impact's result ID is an estimate this run got, and each figure is exactly the estimate's own: name, value, low, high and unit | FR-IMP-004 |
| Every reference the result relies on was returned | FR-INV-004 |

- **Where the rules live:** in this code, not in the JSON schema, so the schema stays one Claude's structured outputs
  take as it is.

**Repairs (SRS 5.6):**
- **What gets sent back:** a draft that breaks a check, or doesn't parse, goes back with its problems listed, at most
  twice.
- **Append-only:** each repair appends the earlier answer and its problems to the conversation, and the history guard
  refuses any request that would edit what came before (LLM-008).
- **Still wrong after two:** the run ends as `failed`, with the problems as its reason. Trying again would only pay
  for the same answer.

**Step 11 checks every recommended action through the Policy Service.**
- **How:** as the Supervisor, with its own token, through WindOps MCP.
- **Outcomes stand:** a `block` or `escalate` stands, and makes `escalation_required` true.
- **If Policy is down:** without a policy result there is no recommendation, and the attempt fails, to be tried
  again (SRS 7.7).

**Step 12 completes the result and keeps it on the run.** It adds:
- the policy outcomes and `policy_result_id`;
- the models that actually answered each role, from the run's call ledger (`model_versions`);
- `generated_at`;
- the run's own data gaps.

The result is kept as `runs.result_json` and returned on `Run.result_json`, for Investigation to take in M8.11.
Drafting a work order from it comes in Phase 9.

**A rehearsal drafts the result without Claude,** from what its tools returned. It still runs the checks and the
real policy check. The graph is now version 3.

## Consequences

- **Proved for free:** `make team-check` rehearses on the cluster. It proves:
  - a result with every SRS 5.7 field, citing only references its tools returned, kept on the run;
  - its recommended action through the real Policy Service, which blocked it, so escalation is required.
- **Proved in unit tests:**
  - every check, including an impact figure changed by one unit or renamed;
  - a repair that appends, never edits;
  - an unparseable answer repaired;
  - a run ended after two failed repairs.
- **Not yet proved:** how well Claude meets these checks, and how often it needs a repair. That is the M8.13 exit
  gate's, and then the evaluations' (Phase 12).
- **What the checks can't catch:** a citation proves a claim rests on a returned record, not that the record says
  what the claim does. Whether a claim is faithful to its source is judged by the evaluators (Phase 12), and by a
  person reviewing the finding (FR-INV-006).
