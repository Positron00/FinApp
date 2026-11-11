# Runbook: BffAvailabilitySlowBurn

| | |
|---|---|
| **Severity** | ticket |
| **Service** | web (the BFF), and the service behind the failing routes |
| **Alert** | `sli:bff_errors:ratio_rate6h > (6 * 0.005) and sli:bff_errors:ratio_rate30m > (6 * 0.005)` for 15 m (`deploy/observability/prometheus/rules/alerts.yml`) |
| **SLO** | 99.5% of J1-J5 BFF requests are not 5xx over 30 days; budget 0.005 (PAY-NFR-04, ADR 0012) |
| **Dashboard** | Journeys, `http://127.0.0.1:4101/d/payrun-journeys` |

## What it means

More than 3% (6 × 0.005) of BFF requests answered 5xx over the last 6 hours and over the last 30 minutes. At this rate the 30-day error budget runs out in about 5 days. It is a steady, low error rate rather than an outage. Alertmanager suppresses this alert while BffAvailabilityFastBurn is firing.

## Impact

Some requests fail for some users, often one route or one kind of request. The SLO will be missed if nothing changes.

## First checks

1. **Which route, over the long window:**

   ```promql
   topk(5, sum by (route, status) (increase(bff_request_duration_seconds_count{route!~"/api/livez|/api/readyz|/api/telemetry", status=~"5.."}[6h])))
   ```

2. **The 30-day picture:** the Journeys dashboard's "Error budget remaining" and burn-rate panels; each journey row has its own burn-rate panel.
3. **Errors and their traces** (Loki):

   ```logql
   {service_name="web"} | event="http.request" | status=~"5.."
   ```

   Group the lines by `route` and open a few `trace_id`s in Tempo.
4. **Upstream errors by target:**

   ```promql
   sum by (target, outcome) (increase(bff_upstream_requests_total{outcome!="ok"}[6h]))
   ```

## Likely causes

- One handler fails for some inputs (500 with `bff.unhandled_error` or the service's `http.unhandled`).
- Timeouts on heavy routes (`/api/pay-runs/[id]/calculate`, `/approve`) under load: 503 `db_timeout`. Compare with the p95 panels for heavy routes.
- An upstream that flaps (breaker opening and closing): `external.breaker.opened` in the logs.
- Redis or Postgres saturation: see the Platform dashboard.

## Mitigation

1. Open a ticket with the failing route, status and example trace IDs.
2. Fix or roll back the faulty handler; for capacity problems, scale the service (Kubernetes HPA limits) or reduce load.
3. If the errors come from a dependency, follow that dependency's runbook (BankErrorRateHigh, RedisMemoryHigh, OutboxStuck).

## Confirm recovery

- The 30-minute ratio drops below 0.03, and the alert resolves after the condition has been false (both windows are needed, so the short window clears it first):

  ```promql
  sli:bff_errors:ratio_rate30m
  ```

- Error budget remaining on the Journeys dashboard stops falling.
