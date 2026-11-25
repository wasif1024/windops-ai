# ADR 0036: Step 14, a decision resumes the case in a run of its own

**Status:** Accepted, 2026-11-25, for M10.4.

## Context

SRS 5.4 ends the investigation at step 13: save the checkpoint, set the case to `pending_review` and end the run.
Step 14 is: resume from a durable event after the human decision, then notify users and record references.

The requirements that apply:
- **AGT-003:** a LangGraph interrupt is not the system of record for a human decision.
- **AGT-004:** each run pins its graph, prompt, model, tool and skill versions, and a resume checks them for
  compatibility.
- **AGT-005:** an incompatible resume stops safely and asks a person to restart from the snapshot.
- **SRS 5.5:** Investigation owns the case's status. The Agent Runtime owns runs and checkpoints. Approval owns the
  decision.

Already in place:
- **The investigation graph (ADR 0018):** it has no interrupt. It ends at step 13, and says the decision "starts a
  run of its own".
- **Following runs (ADR 0025):** Investigation starts each case's run and follows it, setting the case's status.
  It is the only caller the runtime lets in.
- **Decisions (ADR 0032):** the Approval Service announces `approval.decided` with the decision.

The service map had the runtime listening for `approval.decided` itself. That would give the runtime a second way to
start runs and a write path into cases, which ADR 0025 chose against.

## Decision

**Investigation hears the decision and asks for a run.** It listens for `approval.decided` on its own durable
consumer, `investigation-decisions`.
- **The event:** it now carries `case_id`, which the Approval Service has from `workorder.submitted`.
- **Each decision is kept once,** by its approval ID, in `case_decisions`, with a `decided` event in the case's
  progress.
- **Ignored, not dead-lettered:** a decision with no `case_id` (sent before this change), or for a case this service
  does not have. A new consumer replays the stream's last seven days, so old events must not fill the dead letters.

**The keeper resumes the investigation the decision was made on.**
- **The run:** the case's completed investigation run, on the decision's snapshot, resumed in a run of the new
  `decision` workflow.
- **No delegation:** no agent acts in it and it never calls Claude, so it costs nothing.
- **Its key** is the approval's, so two replicas, or a retry, start one run.
- **Order:** a decision made while the case's investigation is still running waits for it to end.
- **Nothing to resume:** where no investigation ran on that snapshot, as for a person's own proposal, the decision is
  recorded and moves the case without a run.

**The `decision` workflow** is a LangGraph graph, checkpointed like the investigation's. It has five steps:

| Step | What it does |
| --- | --- |
| `receive_decision` | Checks the run names its case and snapshot |
| `check_compatibility` | AGT-004: the resumed run is this case's completed investigation, on this snapshot. Its graph version, its pins and its Claude policy are the ones this runtime would use today, and its checkpoint ends at step 13 |
| `restore` | Reads the resumed run's newest checkpoint through a checkpointer that serves only that thread, and never writes |
| `record_references` | Keeps identifiers as the run's result, never evidence: the decision, approval, work order and version, decider, resumed run, its policy results and evidence references, and whether its planner drafted the work order |
| `conclude` | Ends the run |

- **Incompatible:** the worker ends the run `stale`, naming what changed and asking for a restart from the snapshot
  (AGT-005). Nothing is restored.

**Investigation follows the decision run and moves the case.**

| Outcome | Case |
| --- | --- |
| Approved, run completed | `approved`, a new status: the work order waits for the maintenance |
| Rejected, run completed | Stays as it was, normally `pending_review`, for a revised proposal |
| Run ended stale or failed | Back to `open`, with the reason, for a person to restart from the snapshot |
| Case ended, or on newer evidence since | Not moved. The decision is still recorded |

- **History:** each move is a history entry naming the approval, work order and version, announced on
  `case.updated`. That is the notification step: the Notification Service (M10.5) tells people from it.
- **`approved` is active:** detections join an approved case, the stale rules still watch it, and the Approval and
  Maintenance services treat it like any other active case.
- **Migration:** an expand revision adds the wider status constraint, the wider one-active-case index and the
  decisions table. A contract revision drops the narrower constraint and index.

## Consequences

- **The decision stays where it was made.** The case and the resume run hold references to it, never its reasons.
- **The check:** `make resume-check` proves it with the real services, at no cost:
  - a rehearsed investigation, asked for by the demo operator through the gateway, reaches step 13;
  - a planner drafts and submits a work order on the case, and a manager approves it;
  - the case is approved by a decision run, with no delegation and no call to Claude;
  - the run's references name the resumed run, its policy results and evidence;
  - the live stream shows the decision, the run's steps and the case's move.
- **Unit tests cover the other paths:** incompatible pins, another snapshot, a missing run, a rejection, nothing to
  resume, and evidence that moved on.
- **Found on the way:** an investigation a person asks for acts under their delegation. Its planner agent cannot
  draft for the demo operator, who may not draft (ADR 0029), so the run records "no work order" as a data gap. This
  is the designed behavior, which is why the check has a planner draft.
- **Not yet:** a case is not reopened for a second investigation after a stale resume. A person restarts it, as AGT-005
  asks. Post-maintenance verification (M10.6) moves an approved case on from `workorder.completed`.
- **Revisit if:** decisions become frequent enough that a run each costs more than its trace is worth, or the
  runtime gains a second caller.
