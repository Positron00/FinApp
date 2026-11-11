# Runbook: OutboxStuck

| | |
|---|---|
| **Severity** | warning (ticket) |
| **Service** | payroll (outbox relay) |
| **Alert** | `max by (service_name) (outbox_oldest_age_seconds) > 60` for 1 m (`deploy/observability/prometheus/rules/alerts.yml`) |
| **Requirement** | PAY-402 (payout events through the outbox), PAY-NFR-03; R26, C18 |
| **Dashboard** | Queues and workers, `http://127.0.0.1:4101/d/payrun-queues` ("Outbox") |

## What it means

The oldest unsent row in `payroll.outbox` is more than 60 seconds old. The relay normally publishes rows to `stream:payout.requested` within a second or two (SKIP LOCKED, any replica). Payroll computes the age at metric collection: `now() − min(created_at)` over **every** row with `sent_at IS NULL`, including rows the relay refused and will never send.

## Impact

- **Relay or Redis down:** payout events are not reaching the worker, so approved runs do not start paying (PayoutSettlementSlow follows). Nothing is lost: the rows stay until they are sent.
- **A refused row:** the relay is healthy and every other row goes out, but one payout attempt's event was refused as invalid (a producer bug). That payout is stuck until an operator resolves it, and this alert keeps firing until the refused row is removed by hand.

## First checks

1. **Age and backlog:**

   ```promql
   max by (service_name) (outbox_oldest_age_seconds)
   ```

   ```promql
   max by (service_name) (outbox_unsent)
   ```

   A backlog that grows points at the relay or Redis. A small, steady `outbox_unsent` with a steadily rising age points at a refused row.
2. **Relay logs** (Loki):

   ```logql
   {service_name="payroll"} | event=~"outbox.relay.error|outbox.relay.published|redis.unavailable"
   ```

   `outbox.relay.published` stops when the relay is stuck; `outbox.relay.error` gives the reason. A single `outbox.relay.error` with `error_type="EnvelopeInvalidError"` while `outbox.relay.published` carries on is a refused row: it is logged once, not on every poll.
3. **Refused rows** (`scripts/stack.sh psql payroll_app`; read-only):

   ```sql
   SELECT o.id, o.payout_id, o.attempt, o.created_at, o.relay_attempts,
          a.status, a.failure_reason, a.resolved_by
     FROM payroll.outbox o
     LEFT JOIN payroll.payout_attempts a ON a.payout_id = o.payout_id AND a.attempt = o.attempt
    WHERE o.sent_at IS NULL AND o.last_error = 'EnvelopeInvalidError'
    ORDER BY o.created_at;
   ```

4. **Redis:** Platform dashboard (`redis_up`, memory). Redis full (`noeviction`) refuses XADD.

   ```promql
   min(redis_up)
   ```

5. **Payroll gauges:** if `outbox_unsent` has no data at all, the gauge query itself fails (`metrics.gauge_query_failed` in payroll's log): check Postgres.

## Likely causes

- Redis is down or full (RedisMemoryHigh).
- The relay task crashed (`background.task.crashed`), or every payroll replica is down.
- **A refused envelope** (`last_error = 'EnvelopeInvalidError'`): the relay re-validates each envelope before publishing it and refused this one. It never claims the row again, so the row stays unsent and this alert does not clear on its own. Re-publish ignores the refused row: once the attempt's backoff has passed (6 to 15 min after its last publication at the defaults), it moves the attempt to `needs_operator` with reason `envelope_invalid`, which pages PayoutNeedsOperator.

## Mitigation

1. **Relay or Redis:** restore Redis, or restart payroll if the relay task died (`docker compose ... restart payroll`; in Kubernetes, `kubectl rollout restart deployment/payroll`). The backlog then drains by itself. Do not delete these rows: each is a payout event the worker needs.
2. **A refused row:** resolve the attempt first, then delete the row.
   1. **Resolve the attempt.** Wait until the query in "First checks" step 3 shows the attempt as `needs_operator` / `envelope_invalid`, then follow [PayoutNeedsOperator](PayoutNeedsOperator.md): **Send again** only once the bug that produced the bad envelope is fixed and deployed (a redrive writes a new row, which the relay would refuse again), otherwise **Mark rejected** after the bank check. Don't delete the row earlier: while the attempt is still live, re-publish would send a fresh copy of the same bad envelope and the attempt would never get the `envelope_invalid` reason.
   2. **Record the evidence.** Copy the row's `id`, `payout_id`, `attempt` and `created_at` into the ticket, with the `outbox.relay.error` log line.
   3. **Delete exactly that row** as the schema owner, `payroll_migrator` (the payroll service's own role, `payroll_app`, could too, but this is a manual repair; keep the service's credentials for the service). With the Compose stack, `scripts/stack.sh psql payroll_migrator`; elsewhere, connect to the `payrun` database as `payroll_migrator`. Then, with the id from step 3 of "First checks":

      ```sql
      BEGIN;
      SELECT a.status, a.failure_reason, a.resolved_by
        FROM payroll.outbox o
        JOIN payroll.payout_attempts a ON a.payout_id = o.payout_id AND a.attempt = o.attempt
       WHERE o.id = '<outbox_id>';
      -- expect: the attempt resolved (status not needs_operator, resolved_by set); else ROLLBACK
      DELETE FROM payroll.outbox
       WHERE id = '<outbox_id>' AND sent_at IS NULL AND last_error = 'EnvelopeInvalidError';
      -- expect: DELETE 1; anything else: ROLLBACK and stop
      COMMIT;
      ```

      Never delete by any other filter: an unsent row without that `last_error` is a payout event the worker still needs.

## Confirm recovery

- No refused row is left (`scripts/stack.sh psql payroll_app`):

  ```sql
  SELECT count(*) FROM payroll.outbox
   WHERE sent_at IS NULL AND last_error = 'EnvelopeInvalidError';
  ```

  Expect 0.
- The age falls below 60 s at the next collections, and the alert resolves after its 1 m `for` window:

  ```promql
  max by (service_name) (outbox_oldest_age_seconds)
  ```

- The backlog drains (it may not be exactly 0 while runs are being approved):

  ```promql
  max(outbox_unsent)
  ```

- For a relay or Redis outage, `outbox.relay.published` lines resume, and the worker's `queue_messages_total{outcome="ok"}` rate picks up.
