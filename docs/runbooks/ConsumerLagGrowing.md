# Runbook: ConsumerLagGrowing

| | |
|---|---|
| **Severity** | warning (ticket) |
| **Service** | payout-worker |
| **Alert** | `max by (stream, group) (queue_consumer_lag) > 0 and max by (stream, group) (deriv(queue_consumer_lag[10m])) > 0` for 10 m (`deploy/observability/prometheus/rules/alerts.yml`) |
| **Requirement** | PAY-403, PAY-NFR-06 |
| **Dashboard** | Queues and workers, `http://127.0.0.1:4101/d/payrun-queues` ("Consumer lag") |

## What it means

The group `payout-worker` on `stream:payout.requested` has undelivered entries, and that number has been rising for 10 minutes. The worker reads the lag from `XINFO GROUPS` every 15 s (`PAYOUT_LAG_POLL_S`). A burst that drains within a few minutes does not fire.

## Impact

Payouts wait longer and longer before the worker even starts them; the settlement SLO (60 s p95) is being missed.

## First checks

1. **The lag, from the worker and from Redis:**

   ```promql
   max by (stream, group) (queue_consumer_lag)
   ```

   ```promql
   max by (stream, group) (redis_stream_group_lag)
   ```

   If the worker's gauge has no data but the exporter's rises, the worker is down.
2. **Throughput and processing time:**

   ```promql
   sum by (outcome) (rate(queue_messages_total[5m]))
   ```

   ```promql
   histogram_quantile(0.95, sum by (le) (rate(queue_processing_seconds_bucket[5m])))
   ```

   Long processing times usually mean a slow bank (Bank dashboard latency panel).
3. **Worker logs** (Loki):

   ```logql
   {service_name="payout-worker"} | event=~"queue.read.error|queue.message.failed|external.request.retry"
   ```

## Likely causes

- The bank is slow: each message takes seconds and `PAYOUT_CONCURRENCY` (16) slots are full.
- Fewer worker replicas than usual, or a worker stuck in a crash loop.
- An unusual volume: many large runs approved together.

## Mitigation

1. Fix the slow dependency first (BankErrorRateHigh, BankBreakerOpen).
2. Add worker replicas (Kubernetes); the group spreads entries across consumers. Raise `PAYOUT_CONCURRENCY` only with the bank's rate limits in mind.

## Confirm recovery

- The lag stops rising and falls back towards 0:

  ```promql
  max(queue_consumer_lag)
  ```

- Processing p95 returns to its usual range on the Queues dashboard.
