# Runbook: BffAvailabilityFastBurn

| | |
|---|---|
| **Severity** | page |
| **Service** | web (the BFF), and whichever service is behind the failing routes |
| **Alert** | `sli:bff_errors:ratio_rate1h > (14.4 * 0.005) and sli:bff_errors:ratio_rate5m > (14.4 * 0.005)` for 2 m (`deploy/observability/prometheus/rules/alerts.yml`) |
| **SLO** | 99.5% of J1-J5 BFF requests are not 5xx over 30 days; budget 0.005 (PAY-NFR-04, ADR 0012) |
| **Dashboard** | Journeys, `http://127.0.0.1:4101/d/payrun-journeys` |

## What it means

More than 7.2% (14.4 × 0.005) of BFF requests answered 5xx, both over the last hour and over the last 5 minutes. At that rate the whole 30-day error budget is gone in about two days. The SLI counts every BFF route except `/api/livez`, `/api/readyz` and `/api/telemetry` (C4), so a failing health check alone never fires it.

## Impact

Users see errors on one or more journeys: sign-in, employees, pay runs, approval or retries. Money does not move twice because of this alert, but approvals or retries may be failing.

## First checks

1. **Which routes fail.** On the Journeys dashboard, find the row whose "Success (non-5xx) by route" drops. Or query:

   ```promql
   topk(5, sum by (route, status) (rate(bff_request_duration_seconds_count{route!~"/api/livez|/api/readyz|/api/telemetry", status=~"5.."}[5m])))
   ```

2. **Which upstream.** The BFF counts its calls to identity and payroll:

   ```promql
   sum by (target, outcome) (rate(bff_upstream_requests_total{outcome!="ok"}[5m]))
   ```

   and its breakers (2 = open):

   ```promql
   max by (target) (circuit_breaker_state{service_name="web"})
   ```

3. **The BFF's error logs** (Grafana Explore, Loki):

   ```logql
   {service_name="web"} | event=~"bff.upstream_error|bff.unhandled_error|session.backend_error"
   ```

   Open a `trace_id` from a log line to see the failing hop in Tempo.
4. **The upstream service.** If the upstream is payroll or identity, check its own 5xx on the Service overview dashboard (`service` = payroll or identity) and its logs:

   ```logql
   {service_name=~"identity|payroll"} | event=~"db.unavailable|db.timeout|redis.unavailable|http.unhandled"
   ```

5. **Containers.** `scripts/stack.sh ps`: is any service restarting or unhealthy?

## Likely causes

- **identity or payroll is down** or failing its readiness check: the BFF answers 503 `service_unavailable` and its breaker opens (`bff.upstream_error`).
- **Postgres is slow or down:** the service answers 503 `db_timeout` / `db_unavailable` (`db.timeout`, `db.unavailable`). See the Platform dashboard.
- **Redis is down:** the session store fails (`session.backend_error`, `bff_session_backend_errors_total`); sign-in and session checks return 503.
- **A bad deploy:** 500s with `bff.unhandled_error` or `http.unhandled` right after a release.

## Mitigation

1. Restore the failing dependency: start or restart the container (`docker compose ... restart <service>`; in Kubernetes, `kubectl rollout restart deployment/<service>`), or fix Postgres or Redis (see RedisMemoryHigh, Platform dashboard).
2. After a bad release, roll back to the previous image tags (migrations are expand-only, so the old version still works).
3. If one route alone fails and the cause needs a code fix, tell users which journey is affected; the rest of the app keeps working.

## Confirm recovery

- The 5-minute ratio drops below 0.072 at once; the alert resolves when the 5 m window is clean (the `and` needs both windows). Alertmanager sends a `resolved` notification.

  ```promql
  sli:bff_errors:ratio_rate5m
  ```

- The burn-rate panel on the Journeys dashboard falls below the 14.4 line. Watch BffAvailabilitySlowBurn: a long incident can keep the 6-hour window above 6×.
