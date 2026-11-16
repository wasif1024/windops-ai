# ADR 0010: Open-Meteo as the forecast provider, on its free tier for non-commercial use

**Status:** Accepted, 2026-11-16. This confirms the forecast provider vendor check (SRS 13.4).

## Context

The SRS starts the Weather Service with an Open-Meteo adapter (WTH-001), and leaves a vendor check open for Phase 6:
confirm the provider, or add another behind the same contract. Weather feeds safety rules: work at height is decided
on the forecast wind and gusts (FR-POL-004, M6.4). So the provider's terms, what it supplies, and how it fails all
matter.

What Open-Meteo offers, checked on 2026-11-16 against its [terms](https://open-meteo.com/en/terms) and
[documentation](https://open-meteo.com/en/docs):

| | Open-Meteo |
| --- | --- |
| Terms | The free API is for non-commercial use only. Commercial use needs a paid subscription |
| Free limits | 600 calls a minute, 5,000 an hour, 10,000 a day and 300,000 a month |
| Licence | Data under CC BY 4.0, which needs attribution |
| Wind | Speed and direction at 10, 80, 120 and 180 m; gusts at 10 m only |
| Other | Temperature, precipitation, visibility and CAPE hourly; lightning potential only in some regions' 15-minute data |
| Revision | The forecast response doesn't say which model run it comes from |

## Decision

- **The provider:** Open-Meteo, on its free endpoint, for development and the demonstration. Both use synthetic data,
  and neither is commercial.
- **The endpoint is a setting,** `WINDOPS_WEATHER_PROVIDER_URL`, so a subscription for commercial use changes only
  configuration. Any API key it brings is a secret, not a `WINDOPS_` setting, and is added with that subscription.
- **The Weather Service caches each retrieval for an hour,** per tenant and rounded site, well within the free limits:
  the demo fleet's sites need a few dozen calls a day.
- **What it captures:** the hourly wind at 10, 80, 120 and 180 m, gusts at 10 m, direction, temperature, precipitation,
  visibility and CAPE. Hub-height wind is interpolated between the two levels around the hub. Gusts are only known at
  10 m, and every forecast says so by naming its fields.
- **The revision** is recorded as empty, because Open-Meteo doesn't supply one. Nothing is invented (WTH-004).
- **What leaves the cluster:** only coordinates rounded to two decimals, about a kilometre (EXT-006), and each
  request is logged (EGR-004). TLS is always verified.
- **Every forecast names its provider and licence,** CC BY 4.0, for attribution wherever it is shown.

## Consequences

- A commercial deployment needs an Open-Meteo subscription, or another provider behind the same contract, before go
  live. M15.10, the PDPL and data-residency review, is the point to decide it.
- Without the provider, a forecast older than three hours is stale and safety rules block on it. With none at all,
  weather is unavailable, and work at height blocks (SRS 7.7).
- Gusts at hub height are not available from this provider. A safety rule on gusts judges 10 m gusts, and says so.
