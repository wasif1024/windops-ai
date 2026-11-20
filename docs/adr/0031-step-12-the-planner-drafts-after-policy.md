# ADR 0031: Step 12: the Maintenance Planner proposes and drafts one work order, after policy

**Status:** Accepted, 2026-11-20, for M9.3.

## Context

SRS 5.4's step 12 is to "generate the recommendation and, where appropriate, a draft work order". FR-MNT-001 asks the
Maintenance Planner agent to prepare a proposal from verified findings, procedures, weather, history and inventory.

What was already in place:
- **Steps 10 and 11:** step 10 checks the result's citations, and step 11 runs each recommended action through the
  Policy Service (ADR 0022).
- **Drafting:** the Maintenance Service takes drafts from agents, validates them, and lets only a person submit
  (ADR 0029).
- **Parts:** WindOps MCP gives the planner parts to find and check (ADR 0030).
- **Step 12** kept the result, and recorded "draft work orders come in Phase 9" as a data gap.

Two facts shaped the design:
- **Step 5 is too early.** The planner already advises the Supervisor there, before the result is checked or policy
  has run. A draft made then would rest on unchecked findings.
- **A recommendation has no planned window.** The Policy Service's work-at-height rules judge the weather over the
  window, so step 11 blocks every climbing action for want of one (FR-POL-005). The rules' category says so:
  `weather_window`.

## Decision

**Step 12 runs the Maintenance Planner on its own, once the result has passed its checks and policy.** It is the
main agent, with its full allowlist and its skill, from a second, pinned prompt, `draft.md`. It is given:
- the action to plan;
- the result's cited facts, hypotheses, contradictions, data gaps and risk flags;
- the approved procedures the run read (`kn:` citations), the only ones it may cite;
- the time now, after its cached prompt (LLM-007).

It reads the turbine's history, finds and checks parts, drafts one work order through `create_draft_work_order`,
and answers with its proposal.

**"Where appropriate" is the first recommended action that:**
- policy passed or escalated; or
- policy blocked only with `weather_window` rules. The draft has a window, and the Maintenance Service asks the
  Policy Service again over it, on its forecast. A block then stops submission, as any block does.

An action blocked by any other rule gets no draft. A run with no such action records why as a data gap.

**The planner advises in step 5 without its drafting tool.** The team's agents get their allowlists minus
`create_draft_work_order`, so a draft can only come from step 12.

**One draft per run.**
- **A second draft** is refused in the runtime, before it reaches WindOps MCP.
- **The budget:** the planner's tool calls count against what is left of the run's 50.
- **If step 12 is resumed** after its worker stopped mid-step, it runs again and may draft again. An extra draft is
  only a draft, and a person withdraws it.

**What the planner may do is its principal's.**
- **The fault-investigator,** behind fault-triggered investigations, may draft.
- **A person who may not plan,** such as an operator, may not. The Maintenance Service refuses the agent's draft,
  and the run completes with no work order and a data gap saying why, never as a failure.

**Kept on the run's result:**
- `work_order`: its ID, status, revision, the drafting agent, whether its validation passed, its problems, and its
  policy outcome;
- `proposed_action`: the action that was drafted;
- `proposal`: the planner's answer;
- each policy check's `window_only` flag.

**Free paths:**
- **The rehearsal** scripts the planner's step 12: its history, its parts, and a draft of the action, two days out,
  with no parts or procedure.
- **The stand-in API** plays the same turns through the real Claude client (ADR 0027).

**Pins:** the graph is now `investigation@4`. Each run pins the drafting prompt as `maintenance-planner:draft`, and
the planner is now version 4.

## Consequences

- **Proved by `make planner-check`, at no cost, for a fault-triggered case, as a rehearsal and as a dry run:**
  - the planner advised in step 5 without drafting;
  - in step 12 it read the history, found parts, and drafted exactly once;
  - each draft is in the Maintenance Service as the planner agent's, for the fault-investigator, never submitted;
  - the result keeps it, its action and the proposal;
  - its calls were audited as the agent, on the principal's behalf.
- **`make team-check` now shows the other side.** Its operator's planner is refused, and the run completes with a
  data gap.
- **`make workflow-check`** passes on the new graph version.
- **A climbing action is drafted with a window two days out in the rehearsal.** Whether its own policy check then
  passes depends on the real forecast. The checks prove that a draft exists, not what it was judged.
- **Not yet:**
  - **The planner's draft on Claude itself:** the Phase 9 exit gate's, with the user's approval first.
  - **The planner screens:** M9.4.
  - **A person's decision, and the proposal's new versions:** Phase 10.
