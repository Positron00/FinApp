# Runbook: PayoutSettlementSlow

| | |
|---|---|
| **Severity** | page |
| **Service** | payroll (gauge), payout-worker and the bank (usual causes) |
| **Alert** | `max(payroll_runs_processing_oldest_age_seconds) > 60` for 2 m (`deploy/observability/prometheus/rules/alerts.yml`) |
| **SLO** | a 500-employee run settles within 60 s of approval at p95 (PAY-NFR-06) |
| **Dashboard** | Queues and workers, `http://127.0.0.1:4101/d/payrun-queues`; Bank, `/d/payrun-bank` |

## What it means

A pay run has been in `processing` for more than a minute, for at least 2 minutes. The gauge is now minus `processing_since` of the oldest processing run, computed by every payroll replica when metrics are collected (a retry restarts the clock for that run). Some payouts of that run have no final answer yet.

## Impact

Employees are not paid yet. Nothing has been paid twice, but the approver sees the run stuck in "processing", and the settlement SLO is at risk.

## First checks

1. **How old, and since when** (Queues dashboard, "Oldest processing run"):

   ```promql
   max(payroll_runs_processing_oldest_age_seconds)
   ```

2. **Is the worker consuming?** Messages handled, and the backlog:

   ```promql
   sum by (outcome) (rate(queue_messages_total[5m]))
   ```

   ```promql
   max by (stream, group) (redis_stream_group_lag)
   ```

   No `ok` rate and a growing lag mean the worker is down or blocked.
3. **Is the bank answering?** (Bank dashboard):

   ```promql
   sum by (outcome) (rate(external_requests_total{provider="bank"}[5m]))
   ```

   ```promql
   max(circuit_breaker_state{service_name="payout-worker", target="bank"})
   ```

4. **Worker logs** (Loki):

   ```logql
   {service_name="payout-worker"} | event=~"external.breaker.opened|bank.transfer.no_answer|queue.message.failed|queue.read.error"
   ```

5. **Which run.** As an approver in the web app (Pay runs list), or `GET /v1/pay-runs?status=processing` on payroll. Note the run ID and the payouts that are not final.

## Likely causes

- **The bank is down or slow:** `connect_error` / `timeout` outcomes, the bank breaker open (BankBreakerOpen follows after 5 minutes), attempts go `indeterminate` and wait for re-publish (`PAYOUT_REPUBLISH_AFTER_S`, 360 s).
- **payout-worker is down** or crash-looping: no `queue_messages_total` rate; `scripts/stack.sh ps`.
- **Redis is down:** the outbox cannot publish (OutboxStuck) and the worker cannot read.
- **payroll is failing the worker's calls** (instruction or result): `external_requests_total{provider="payroll"}` errors on the Bank dashboard.

## Mitigation

1. Restore the failing component: start the worker (`docker compose ... start payout-worker`), fix the bank connection, or fix Redis.
2. Do **not** retry payouts by hand while the bank is unreachable: the bank key `<payout_id>:<attempt>` makes the worker's own retries safe, and payroll re-publishes non-final attempts on its own (PAY-407).
3. If attempts end in `needs_operator`, follow PayoutNeedsOperator.

## Confirm recovery

- The gauge falls to 0 once every run reaches `paid` or `partially_failed`:

  ```promql
  max(payroll_runs_processing_oldest_age_seconds)
  ```

- The run shows a final status in the web app, and `payrun.completed` appears in the logs:

  ```logql
  {service_name="payroll"} | event="payrun.completed"
  ```
