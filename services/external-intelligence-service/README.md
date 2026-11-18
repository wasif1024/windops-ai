# windops-external-intelligence

Web research through Tavily (SRS 6.1, EXT-001 to EXT-006): forecasts, severe-weather notices, OEM bulletins, technical
advisories, and industry, regulatory and environmental updates. It is advisory only: no safety rule takes its input
from here (FR-POL-004). The decisions behind it are in
[ADR 0013](../../docs/adr/0013-web-research-made-generic-and-advisory.md).

## A search

`Research` takes a topic, the caller's words, and optionally the wind farm or turbine it concerns. It needs
`knowledge.read`, for that wind farm when one is named.

| Step | What happens |
| --- | --- |
| Made generic (EXT-006) | The tenant's, its sites' and turbines' IDs and names, serial numbers, model designations, record IDs, e-mail and web addresses are taken out. A site goes as its region, such as "offshore, United Kingdom, near 55°N 2°E", and a turbine as its class, such as "8 MW offshore wind turbine". A credential refuses the query |
| Within the policy (EXT-005) | The tenant's research must be on, within its daily limit, 200 calls by default, and 20 calls a minute. Its allowed and blocked domains go with the search, beside the platform's own blocklist of user-generated sites |
| Searched | Tavily, with a 15-second timeout behind a circuit breaker |
| Assessed (EXT-004) | Each source is flagged `stale`, `undated`, `low_confidence`, `conflicting` or `instructions_to_ai`, each with a note. Sources from authoritative domains are marked `trusted` |
| Recorded (EXT-003) | The caller's words and exactly what was sent, what kinds of thing were taken out, the region and class, the provider's request ID and timing, and every source with its URL, title, content, score, publication and retrieval times. It never changes, and is audited |

When research is off, over a limit or Tavily cannot be reached, the answer is `unavailable`, with the data gap stated
and recorded, never an error: an investigation carries on without it (SRS 7.7, EGR-002).

`Extract` fetches the full content of up to five sources an earlier search found, never any other address, under
the same policy and limits. `GetResearch` reads a search and its extractions; a search made for a wind farm the
caller may not read is not found.

## The research policy

`GetResearchPolicy` needs `knowledge.read`. `SetResearchPolicy` needs `knowledge.manage`, a person and a recent second
factor, and is audited. It turns research on or off, sets the allowed and blocked domains, up to 100 each, and the
daily limit.

## What leaves the cluster

Only the generic query and the domain policy go to Tavily. Each request is logged by its kind and size, never its text
(EGR-004). TLS is always verified. The key is `TAVILY_API_KEY` in `.env`: `make secrets` makes it the Secret
`windops-tavily-api-key`, which only this service's pod mounts, as a file read at each request (EXT-005). Without it,
research is a data gap.

The service reads the tenant's wind farms and turbines from the Asset Service as itself, with its fixed grant of
`turbine.read` (ADR 0008), and the tenant's name from the Tenant Service, for the names it takes out.

## Run the tests

```bash
uv run pytest services/external-intelligence-service/tests
make research-check   # on the cluster: real Tavily searches, redaction, flags, policy and PostgreSQL
```

## Build the image

```bash
docker build -f services/external-intelligence-service/Dockerfile -t windops-external-intelligence:local .
```
