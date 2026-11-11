# Runbook: PayoutDLQ

| | |
|---|---|
| **Severity** | page (money may be stuck) |
| **Service** | payout-worker |
| **Alert** | `increase(queue_messages_total{outcome="dlq"}[15m]) > 0` (`deploy/observability/prometheus/rules/alerts.yml`) |
| **Requirement** | PAY-408, PAY-409, PAY-NFR-11; DLQ entry contract `contracts/events/payout.requested.dlq.v1.json` (C21) |
| **Dashboard** | Queues and workers, `http://127.0.0.1:4101/d/payrun-queues` |

## What it means

The worker moved at least one message from `stream:payout.requested` to `stream:payout.requested.dlq` in the last 15 minutes, then acknowledged it. A message is dead-lettered:

- **at once** when it is poison (`error_kind = invalid`): schema-invalid (`schema_invalid`), the payout does not exist (`payout_not_found`), or the instruction's amount, currency or IDs differ from the event (`amount_mismatch`). When the IDs are readable the worker first reports the attempt as poison, which moves it to `needs_operator` (PayoutNeedsOperator follows);
- **after `PAYOUT_MAX_DELIVERIES` (5) deliveries** when payroll keeps failing (`error_kind = payroll`) or the worker hits an internal error (`internal`). Those attempts stay `requested`, and payroll re-publishes them after `PAYOUT_REPUBLISH_AFTER_S` (360 s).

The counter is pre-initialised at 0 (C21), so the first dead letter after a restart fires the alert. It clears 15 minutes after the last one.

## Impact

The payout in the message is not being paid until payroll re-publishes it or an operator resolves it. For poison messages a person must decide.

## First checks

1. **How many, and why** (Queues dashboard, "Dead-lettered messages"):

   ```promql
   sum by (stream) (increase(queue_messages_total{outcome="dlq"}[15m]))
   ```

   ```promql
   max by (stream) (queue_dlq_messages)
   ```

2. **The worker's log line for each dead letter** carries `error_kind`, `error_code`, `delivery` and `source_id`:

   ```logql
   {service_name="payout-worker"} | event="queue.message.failed" | dlq="true"
   ```

3. **The DLQ entries themselves** (dev; use the admin user, which can read every key):

   ```sh
   scripts/stack.sh ps    # the stack must be up
   docker compose --env-file .env -f deploy/compose/compose.yaml exec -e REDISCLI_AUTH="$REDIS_ADMIN_PASSWORD" \
     redis redis-cli XRANGE stream:payout.requested.dlq - + COUNT 20
   ```

   Each entry has `envelope, source_id, error_kind, error_code, error, delivery_count, consumer, failed_at`. Never paste envelopes into tickets: they carry amounts.
4. **The payout in payroll:** from the envelope's `data.payout_id`, check the attempt's status in the web app (run page, payout history) or `GET /v1/pay-runs/{pay_run_id}/payouts`.

## Likely causes

- `error_kind = payroll`: payroll was down, or returned 5xx/4xx to the worker for 5 deliveries (`external_requests_total{provider="payroll"}` errors on the Bank dashboard).
- `error_kind = internal`: the worker's own database failed or `HANDLER_DEADLINE_S` was exceeded.
- `error_kind = invalid`: a bad event (a code bug in the producer, a hand-written stream entry) or a payout that payroll does not know.

## Mitigation

**Replay or purge, by error kind:**

1. **`payroll` or `internal`:** fix the cause first (payroll up, worker DB healthy). Then you usually need do nothing: payroll re-publishes every non-final attempt after 360 s with a fresh `event_id` and the same bank key, so the bank pays at most once. To replay sooner, re-add the stored envelope to the main stream as the admin user, then delete the DLQ entry:

   ```sh
   # <envelope> is the entry's envelope field; event_id and type come from inside it
   redis-cli XADD stream:payout.requested MAXLEN '~' 100000 '*' event_id <event_id> type payout.requested envelope '<envelope>'
   redis-cli XDEL stream:payout.requested.dlq <dlq-entry-id>
   ```

   A replayed `event_id` is processed again (dead letters are never recorded in `processed_events`), and the bank key keeps it to one transfer.
2. **`invalid` (poison):** do **not** replay. If the attempt is now `needs_operator`, follow PayoutNeedsOperator. If the payout does not exist (`payout_not_found`), find where the message came from; it is safe to delete.
3. **Purge** an entry only once its payout is final or resolved: `XDEL stream:payout.requested.dlq <id>`. The DLQ is capped at about 10000 entries (`MAXLEN ~ 10000`), so old entries are trimmed eventually; don't rely on that for live payouts.

## Confirm recovery

- No new dead letters: the 15-minute increase returns to 0 and the alert resolves.

  ```promql
  sum(increase(queue_messages_total{outcome="dlq"}[15m]))
  ```

- The affected payouts reach `paid` or `failed` (or are resolved), and `payroll_attempts_needs_operator` returns to 0.
