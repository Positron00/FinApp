# Runbook: OutboxStuck

| | |
|---|---|
| **Severity** | warning (ticket) |
| **Service** | payroll (outbox relay) |
| **Alert** | `max by (service_name) (outbox_oldest_age_seconds) > 60` for 1 m (`deploy/observability/prometheus/rules/alerts.yml`) |
| **Requirement** | PAY-402 (payout events through the outbox); R26, C18 |
| **Dashboard** | Queues and workers, `http://127.0.0.1:4101/d/payrun-queues` ("Outbox") |

## What it means

The oldest unsent row in `payroll.outbox` is more than 60 seconds old. The relay normally publishes rows to `stream:payout.requested` within a second or two (SKIP LOCKED, any replica). Payroll computes the age at metric collection.

## Impact

Payout events are not reaching the worker, so approved runs do not start paying (PayoutSettlementSlow follows). Nothing is lost: the rows stay until they are sent.

## First checks

1. **Age and backlog:**

   ```promql
   max by (service_name) (outbox_oldest_age_seconds)
   ```

   ```promql
   max by (service_name) (outbox_unsent)
   ```

2. **Relay logs** (Loki):

   ```logql
   {service_name="payroll"} | event=~"outbox.relay.error|outbox.relay.published|redis.unavailable"
   ```

   `outbox.relay.published` stops when the relay is stuck; `outbox.relay.error` gives the reason.
3. **Redis:** Platform dashboard (`redis_up`, memory). Redis full (`noeviction`) refuses XADD.

   ```promql
   min(redis_up)
   ```

4. **Payroll gauges:** if `outbox_unsent` has no data at all, the gauge query itself fails (`metrics.gauge_query_failed` in payroll's log): check Postgres.

## Likely causes

- Redis is down or full (RedisMemoryHigh).
- The relay task crashed (`background.task.crashed`), or every payroll replica is down.
- A refused envelope (`envelope_invalid`) is retried: the relay moves that attempt to `needs_operator` (C71) and continues.

## Mitigation

1. Restore Redis, or restart payroll if the relay task died (`docker compose ... restart payroll`; in Kubernetes, `kubectl rollout restart deployment/payroll`).
2. Do not delete outbox rows: each is a payout event the worker needs.

## Confirm recovery

- The age falls below 60 s and the backlog drains:

  ```promql
  max(outbox_unsent)
  ```

- `outbox.relay.published` lines resume, and the worker's `queue_messages_total{outcome="ok"}` rate picks up.
