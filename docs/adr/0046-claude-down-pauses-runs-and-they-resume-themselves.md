# ADR 0046: Claude down pauses runs, and they resume by themselves

**Status:** Accepted, 2026-11-28, for M13.2b.

## Context

M13.2's fault injection found three SRS 7.7 behaviors that were defined but never built.

- **Anthropic API:** "Pause the run with a resumable status and notify the operator, without inventing findings."
  No breaker guarded Claude. An outage failed the run's attempt, the run was tried again later, and after three
  attempts it failed for good.
- **Tenant:** "Use the last known branding." The console fell back to the default branding.
- **LangSmith:** "Buffer or drop traces with an alert." The client buffered and dropped traces, but nothing said so.

## Decision

**Claude has a breaker, one per runtime replica, shared by every run.**
- Every call to Claude goes through it. A failed connection or timeout becomes `ClaudeUnreachableError`, a
  `ConnectionError`, so it counts as a failure, as do Anthropic's 429, 529 and other 5xx answers. A bad request, a
  refusal or an exhausted budget is the run's own, and does not count.
- It opens on 3 failures in its last 10 calls, not SRS 7.7's starting 10 in 20, because a run makes few calls. Only a
  call past 4 minutes counts as slow, as an answer can take minutes and requests stop at 5.
- The Anthropic SDK's own two retries stay, so each call through the breaker is at most three attempts, and nothing
  retries above it (RES-003).

**A run that finds Claude down pauses instead of failing.**
- The worker pauses the run when its error, or any error it wraps, is Claude being down. The run keeps its
  checkpoints, and its attempt is given back. It is paused as `service:agent-runtime`, with a reason that says so, and
  audited as `agent_run.paused`.
- A person may resume it at once, as any paused run. Otherwise, every 30 seconds an idle worker checks Claude's
  breaker. Once it is no longer open, every run paused for Claude is queued again, audited as `agent_run.resumed`.
  Those runs are the breaker's probes: if Claude is still down, they pause again.
- A run a person paused is never resumed this way.
- `windops_agent_runs_paused_claude_down_total` counts the pauses, and `WindopsAgentRunsPausedClaudeDown` tells the
  operator.

**The console keeps each tenant's last known branding.**
- Each branding the console loads is kept in the browser, by tenant. When the Tenant Service cannot be reached, the
  console shows that tenant's last known branding, checked as an answer from the API is. It never shows another
  tenant's, and shows no logo, since logos come from the Tenant Service too. With nothing kept, it uses the defaults.

**Dropped traces are counted.**
- The LangSmith client only logs a failed send, on `langsmith.client`, and drops its traces. A log handler counts
  those messages in `windops_agent_traces_dropped_total`, and `WindopsAgentTracesDropped` alerts.
- A test fails if a newer LangSmith words those messages differently.

## Consequences

- An outage of Claude no longer spends runs' attempts or fails investigations. They wait, and carry on from their
  last checkpoint when Claude answers. Nothing is produced meanwhile.
- `make chaos` covers every SRS 7.7 row, with no gaps.
- A paused run still holds its place against the queue's age limit only while queued. A long outage leaves runs
  paused, not stale, until Claude returns or a person cancels them.
