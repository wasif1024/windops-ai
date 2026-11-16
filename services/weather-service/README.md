# windops-weather

Structured weather forecasts for a turbine's site, normalized behind one contract (SRS 6.2, WTH-001 to WTH-005),
starting with Open-Meteo ([ADR 0010](../../docs/adr/0010-open-meteo-as-the-forecast-provider.md)). Policy's safety
rules take their weather from here and nowhere else (FR-POL-004, M6.4).

## A forecast

`GetForecast` takes a turbine and a window, up to 7 days ahead and 2 days back; the next 24 hours by default. The
service reads the turbine's coordinates and hub height from the Asset Service, as itself (`turbine.read`).

| Field | What it holds |
| --- | --- |
| `state` | `fresh`, `stale` or `unavailable`, with a `note` saying why, and `age_seconds` (WTH-005) |
| `forecast` | The provider and its licence, the model, the revision when given, when it was retrieved, the span it is valid for, the rounded coordinates sent, the elevation, the hub height, and the sha256 of the provider's own answer (WTH-003) |
| `points` | Each hour: wind at 10 m and at the hub, gusts and direction at 10 m, temperature, precipitation, visibility and CAPE, each only when the provider gave it (WTH-004) |
| `summary` | The window's highest hub wind, gust and 10 m wind, total precipitation, lowest visibility and highest CAPE, and how many of the hours asked for the forecast covers |

Hub-height wind is interpolated between the provider's two levels around the hub; without both, it is absent, never
extrapolated.

## Fresh, stale or unavailable

- **Kept for an hour:** each retrieval is kept, per tenant and rounded site, and used for an hour before the provider
  is asked again.
- **When the provider can't be reached:** the newest kept forecast comes back, with its age. Once it is 3 hours old
  it is `stale`, the same limit Policy's weather rules use, and stale data is never presented as current.
- **With nothing kept:** the answer is `unavailable`.
- **Counted by the breaker:** a 429, a 5xx, a timeout or no connection counts against the provider's circuit breaker.
  A refusal or an unreadable answer is unavailable, but isn't counted as the provider failing.
- **Never changed:** a kept forecast never changes, so a policy result can point to the forecast it judged on.

## Who may read it

`weather.read` for the tenant, the turbine's wind farm or the turbine: Operations Managers, Control Room Operators,
Reliability Engineers, Maintenance Planners and Maintenance Managers. A turbine someone may not read is not found.

## What leaves the cluster

Only the site's coordinates, rounded to two decimals, about a kilometre, go to the provider: no tenant, turbine or
case (EXT-006). Each request is logged with the rounded coordinates (EGR-004), and TLS is always verified. The
endpoint is `WINDOPS_WEATHER_PROVIDER_URL`. The free one is for non-commercial use only.

## Run the tests

```bash
uv run pytest services/weather-service/tests
make weather-check   # on the cluster: a real Open-Meteo forecast, the cache, who may read it, and PostgreSQL
```

## Build the image

```bash
docker build -f services/weather-service/Dockerfile -t windops-weather:local .
```
