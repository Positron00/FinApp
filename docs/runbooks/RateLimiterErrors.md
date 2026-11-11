# Runbook: RateLimiterErrors

| | |
|---|---|
| **Severity** | warning (ticket) |
| **Service** | the service in the alert's `service_name` label (web, identity, payroll) |
| **Alert** | `sum by (service_name, component) (rate(rate_limiter_errors_total[5m])) > 0` for 5 m (`deploy/observability/prometheus/rules/alerts.yml`) |
| **Requirement** | PAY-603 (rate limits), C63 |
| **Dashboard** | Service overview, `http://127.0.0.1:4101/d/payrun-service-overview` ("Rate limiting") |

## What it means

A rate limiter cannot reach Redis, steadily for 5 minutes. The counter is pre-initialised at 0 per component, so any error shows. Components: `web` (the BFF's IP and session limits), `login`, `accept` and `reset` (identity), `caller` (every service's per-caller limit). On a Redis error:

- the **login, reset and accept** limiters **fail closed**: those requests get 503;
- the other limiters **fail open**: requests pass without a limit.

## Impact

With fail-closed components (`login`, `accept`, `reset`) users cannot sign in, accept invites or reset passwords. With fail-open components the service runs without abuse protection.

## First checks

1. **Which service and component:**

   ```promql
   sum by (service_name, component) (rate(rate_limiter_errors_total[5m]))
   ```

2. **The service's log** (one line per 10 s per limiter):

   ```logql
   {service_name=~"web|identity|payroll"} | event="ratelimit.backend_error"
   ```

   `error_type` says how Redis failed; `fail_mode` whether requests are refused.
3. **Redis health:** Platform dashboard (`redis_up`, memory, clients):

   ```promql
   min(redis_up)
   ```

4. **Is it only this service?** Session-store errors at the BFF (`bff_session_backend_errors_total`) or `redis.unavailable` logs elsewhere point at Redis itself rather than one service's connection.

## Likely causes

- Redis is down, restarting, or out of memory (RedisMemoryHigh: with `noeviction`, writes fail).
- The service's Redis ACL user or password changed (`NOAUTH` / `WRONGPASS` errors).
- Network trouble between the service and Redis (Toxiproxy toxics in tests).

## Mitigation

1. Restore Redis (start the container, free memory, fix the failover).
2. For a credentials error, fix the service's Redis password from its secret and restart it.
3. If sign-ins are refused for long, tell users; do not switch the login limiter to fail open (PAY-603).

## Confirm recovery

- The error rate returns to 0 for each component and the alert resolves after 5 minutes:

  ```promql
  sum by (service_name, component) (rate(rate_limiter_errors_total[5m]))
  ```

- No new `ratelimit.backend_error` lines.
