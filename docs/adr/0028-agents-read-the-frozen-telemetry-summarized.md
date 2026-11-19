# ADR 0028: Agents read the frozen telemetry, summarized by the service that owns it

**Status:** Accepted, 2026-11-20, for M9.0.

## Context

The Phase 8 exit gate's investigation on Claude named the right fault, but marked every hypothesis untested
([evidence](../phases/phase-08-exit-gate.md)):
- **No readings:** a case's evidence snapshot names its frozen telemetry snapshot, with sample counts and quality
  labels, but no readings.
- **Only the latest point:** agents could read each metric's newest value, never the frozen window.
- **Too much raw data:** the Telemetry Service can stream every frozen point, but a two-hour window is about 86,000 of
  them, far too many for a model.

The same run showed that a forecast an agent read could not be cited.

## Decision

**The Telemetry Service summarizes a frozen snapshot, deterministically.** `SummarizeTelemetrySnapshot` returns, for
each metric:
- the readings' count, then good, suspect and missing;
- the minimum, maximum, mean, first and last of the readings that have a value;
- the least-squares trend per hour;
- the mean of each of up to 48 equal slices of the window.

How it works:
- **One pass:** it goes over the frozen samples in their order, so the same snapshot always gives the same summary.
- **Missing readings:** counted, never averaged.
- **Permission:** it needs `telemetry.read`, as the snapshot does.

The service that owns the data computes it, so no agent and no MCP server works a figure out.

**WindOps MCP's `get_telemetry_summary` gives it to agents.**
- **Its input:** the telemetry snapshot's ID, as the evidence item names it.
- **Citations:** each metric's summary has a `record_id`, `<snapshot>#<metric>`, which a claim cites like any
  evidence item's.
- **Who gets it:** the SCADA Analyst and the Reliability Engineer have it on their allowlists, and their prompts say
  when to use it.

**A forecast may be cited.** `forecast_id` joins the keys whose values a claim may cite.

**Found while proving it:**
- **The servers' keepalive setting was misspelt.** ADR 0027 gave every server `min_recv_ping_interval…`, a name gRPC
  ignores without a word. Servers kept their five-minute default and answered the channels' 20-second pings with
  GOAWAY "too_many_pings", dropping idle connections.
  - **The fix:** gRPC's own name, `grpc.http2.min_ping_interval_without_data_ms`.
  - **A test** checks every option name against the gRPC library itself.
- **`estimate_impact` always failed through WindOps MCP.** An estimate takes about 7 seconds, and the MCP server gave
  every call 5. It now gets 15, inside the tool's own 20.

## Consequences

- **Proved on the cluster:** `make agent-tools-check` has the SCADA Analyst summarize a real case's frozen
  telemetry, about 7,200 readings, each figure citable.
- **Agents can test their hypotheses against the frozen readings.** A paid run to see that is not part of this
  module. It needs the person responsible's approval first.
- **Not yet:** raw points for a short slice, such as for a vibration spectrum. The Sandbox tasks take values an agent
  gives them, and a summary's slices are the first source of those.
