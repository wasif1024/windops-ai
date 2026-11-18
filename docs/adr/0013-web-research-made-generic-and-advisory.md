# ADR 0013: Web research, made generic, recorded and advisory

**Status:** Accepted, 2026-11-18, for M7.4.

## Context

The External Intelligence Service integrates Tavily search and extraction (EXT-001) for forecasts, severe-weather
notices, OEM bulletins, technical advisories, and industry, regulatory and environmental updates (EXT-002). The SRS
requires that it:
- records each source's provenance (EXT-003);
- flags stale, undated, conflicting and low-confidence sources (EXT-004);
- applies quotas, timeouts, rate limits, a domain policy and secret isolation (EXT-005);
- sends only generic queries, never tenant names or case details (EXT-006).

When Tavily is unavailable, investigations continue, recording a data gap (SRS 7.7), and each tenant can turn the path
off (EGR-002). Tavily is advisory only: no safety rule takes its input from it (FR-POL-004).

The SRS leaves open who may research, how a query is made generic, and what "conflicting" means.

## Decision

**Who may research:**
- **`knowledge.read`:** research is reference material, as documents are. A caller scoped to one wind farm researches
  only in its context, by naming it or one of its turbines. A search made for a wind farm is not found by those who
  may not read there.
- **`knowledge.manage`, a person, and a second factor** for the tenant's research policy: on or off, allowed and
  blocked domains, and the daily limit. No new permission is added to SRS 3.2.

**What goes out (EXT-006):**
- **Taken out of the caller's words:** the tenant's ID and name; its wind farms' and turbines' IDs and names; the
  turbines' serial numbers and model designations; any platform record ID; e-mail addresses; web addresses.
  - Each is matched whole, regardless of case, a hyphenated ID also as words.
  - Only the kind of thing taken out is recorded.
  - A query holding a credential is refused, and so is one with nothing generic left.
- **A site** goes as its approximate region: sea or land, country, and the nearest whole degree, such as "offshore,
  United Kingdom, near 55°N 2°E".
- **A turbine** goes as its generic class, such as "8 MW offshore wind turbine", never its model.
- **The response shows exactly what was sent,** beside the caller's words, which stay in the platform.

**What comes back (EXT-003, EXT-004):**
- **Recorded as given:** every search, with its URL, title, content, the provider's score and request ID, retrieval
  and publication times, region, class and both queries. It never changes. Extractions are kept the same way, and
  only of sources an earlier search found, never an arbitrary address.
- **Flags,** each with a note, and nothing dropped for one:
  - **stale:** older than its topic allows, from 6 hours for a forecast to 3 years for an OEM bulletin.
  - **undated:** the provider could not tell when it was published.
  - **low_confidence:** a weak match, below 0.5, from a domain not on the authoritative list.
  - **conflicting:** for forecasts and severe weather, sources giving different warning levels.
  - **instructions_to_ai:** its text addresses an AI. It is only text, and M7.5 wraps it as untrusted.
- **Domains:** user-generated sites are never searched. A tenant may block more, or allow only a list.

**Limits (EXT-005):**
- **Quotas:** 200 provider calls a UTC day per tenant by default, and 20 a minute, so a runaway loop spends a
  minute's worth.
- **Timeouts:** 15 seconds for a search and 30 for an extraction, behind a circuit breaker.
- **The key:** `TAVILY_API_KEY` in `.env`. `make secrets` makes it the Secret `windops-tavily-api-key`, mounted only
  in this service's pod and read at each request.
- **Over a limit, turned off or unreachable:** the answer is `unavailable`, with the data gap stated and recorded,
  never an error.

## Consequences

- **"Conflicting" covers warning levels only.** Other disagreements between sources are for the agents and the
  Validation step to weigh (Phase 8), with every source's text and date in front of them.
- **The region is coarse on purpose.** Tavily cannot narrow a search to one site, which is the point.
- **Tavily sees only generic queries.** Its own terms on retention apply to those, and to nothing of a tenant's.
