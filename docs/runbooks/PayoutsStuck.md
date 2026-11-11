# Runbook: PayoutsStuck

| | |
|---|---|
| **Severity** | page |
| **Service** | payroll (gauge), payout-worker, Redis, the bank |
| **Alert** | `max(payroll_payouts_stuck) > 0` for 10 m (`deploy/observability/prometheus/rules/alerts.yml`) |
| **Requirement** | PAY-407 (re-publish), PAY-NFR-11 |
| **Dashboard** | Queues and workers, `http://127.0.0.1:4101/d/payrun-queues` |

## What it means

At least one payout attempt has been non-final (`requested`, `indeterminate` or `needs_operator`) for longer than `PAYOUT_STUCK_AFTER_S` (600 s), and that has lasted 10 more minutes. Payroll computes the count at metric collection; `max()` collapses replicas. Re-publish (after `PAYOUT_REPUBLISH_AFTER_S`, 360 s, with backoff up to 900 s) has not moved them on.

## Impact

Employees are not paid 20 minutes or more after approval. The run stays `processing` (PayoutSettlementSlow is usually firing too).

## First checks

1. **How many, and is it changing:**

   ```promql
   max(payroll_payouts_stuck)
   ```

   ```promql
   sum(increase(payroll_payouts_republished_total[30m]))
   ```

   Re-publishes rising while the count stays flat means the events go out but nothing answers.
2. **Is the worker consuming?**

   ```promql
   sum by (outcome) (rate(queue_messages_total[5m]))
   ```

   ```promql
   max by (stream, group) (redis_stream_group_lag)
   ```

3. **Is the bank answering?** Bank dashboard: `external_requests_total{provider="bank"}` by outcome and the breaker state.
4. **Logs** (Loki):

   ```logql
   {service_name=~"payroll|payout-worker"} | event=~"payout.republished|payout.indeterminate|queue.group.recreated|queue.read.error|outbox.relay.error"
   ```

5. **Part of it may be `needs_operator`:** if `payroll_attempts_needs_operator` is above 0, those attempts need a person (PayoutNeedsOperator).

## Likely causes

- **payout-worker is down** or cannot read the stream (`queue.read.error`): `scripts/stack.sh ps`.
- **The bank is down for a long time:** attempts cycle through `indeterminate` and re-publish.
- **Redis lost its data** (FLUSHALL, failover without persistence): the consumer group was recreated (`queue.group.recreated`); re-publish redrives every non-final attempt, so this clears on its own within one re-publish cycle.
- **The outbox is not publishing:** OutboxStuck fires too.

## Mitigation

1. Restore the failing component: start the worker, fix the bank connection or Redis.
2. Do not retry or mark payouts by hand while they are `requested` or `indeterminate`: re-publish uses the same bank key, so the bank pays each attempt at most once.
3. For attempts in `needs_operator`, follow PayoutNeedsOperator.

## Confirm recovery

- The count returns to 0 and the alert resolves:

  ```promql
  max(payroll_payouts_stuck)
  ```

- `payout.result_recorded` lines appear for the affected payouts and the run reaches a final status:

  ```logql
  {service_name="payroll"} | event=~"payout.result_recorded|payrun.completed"
  ```
