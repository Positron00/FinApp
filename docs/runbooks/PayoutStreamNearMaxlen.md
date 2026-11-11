# Runbook: PayoutStreamNearMaxlen

| | |
|---|---|
| **Severity** | warning (ticket) |
| **Service** | redis (`stream:payout.requested`), payout-worker |
| **Alert** | `max(redis_stream_length{stream="stream:payout.requested"}) > 0.8 * 100000` for 5 m (`deploy/observability/prometheus/rules/alerts.yml`) |
| **Dashboard** | Queues and workers, `http://127.0.0.1:4101/d/payrun-queues` ("Stream length") |

## What it means

The payout stream holds more than 80000 entries, 80% of `STREAM_MAXLEN` (100000). The relay adds with `MAXLEN ~ 100000`, so once the stream is full Redis trims the oldest entries, even ones the worker has not read yet. The length counts read and acknowledged entries too (Redis keeps them until trimmed), so a long stream alone is not a backlog; the group lag is.

## Impact

None yet. If unread entries are trimmed, those payouts wait for payroll's re-publish (360 s and more), which delays pay but never pays twice.

## First checks

1. **Length and backlog:**

   ```promql
   max by (stream) (redis_stream_length)
   ```

   ```promql
   max by (stream, group) (redis_stream_group_lag)
   ```

   High length with a small lag: the worker keeps up, the stream is just long. High lag: the worker is behind (ConsumerLagGrowing).
2. **Worker throughput:**

   ```promql
   sum by (outcome) (rate(queue_messages_total[5m]))
   ```

3. **Worker logs:**

   ```logql
   {service_name="payout-worker"} | event=~"queue.message.trimmed|queue.read.error|queue.group.recreated"
   ```

## Likely causes

- A very large run or many runs approved at once, with the worker at `PAYOUT_CONCURRENCY` (16 per replica).
- The worker is down or slowed by the bank (see PayoutSettlementSlow, BankBreakerOpen).
- Normal growth: the stream keeps acknowledged entries until `MAXLEN` trims them.

## Mitigation

1. If the lag is high, add worker replicas (Kubernetes) or fix the bank or worker problem first.
2. If the lag is near 0, nothing is at risk; the trim keeps the length near 100000. Note it in the ticket.
3. Do not trim the stream by hand while the lag is above 0.

## Confirm recovery

- The length stays below 80000, or the lag stays near 0:

  ```promql
  max(redis_stream_length{stream="stream:payout.requested"})
  ```

- No `queue.message.trimmed` warnings in the worker logs.
