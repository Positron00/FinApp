# 0003. Payout flow: event out, API back; double-payment guard

Status: accepted 2026-11-10
Context:
- The intake brief sketched `payout.requested` going out and `payout.succeeded`/`payout.failed` events coming back.
- The skill's rule is that a worker which changes another service's data calls that service's API, with `Idempotency-Key: <event_id>`.
- The worker needs bank details. Putting account numbers in a Redis stream would spread sensitive data into a store with weaker controls.
- The invariants:
  - per `payout_id`, at most one settled bank transfer across all attempts;
  - every payout reaches a final state once the bank answers;
  - nothing depends on the stream as the system of record.
Options:
1. **Event out, API back.**
   - payroll publishes `payout.requested` (IDs and amount only).
   - On **every delivery**, the worker fetches the instruction (decrypted bank details) from payroll's internal API, calls the bank, and posts the result to payroll's internal API.
   - The worker's own schema holds only `processed_events` and `disbursements`.
2. **Events both ways** (the original brief). The worker would need its own outbox and payroll a consumer group. That's an extra stream and relay, and it still needs a synchronous fetch for bank details, or puts them in the event.
3. **Bank details in the event.** No synchronous fetch, but account numbers sit in Redis and in the DLQ.
**Double-payment guard (in option 1):**
- (a) **One bank key per attempt.** The bank call carries `Idempotency-Key = <payout_id>:<attempt>`. The processor (bank-stub: a persistent `transfers` table with `UNIQUE(idempotency_key)`, written with INSERT … ON CONFLICT) returns the original transfer for a repeated key, across restarts, replicas and concurrent POSTs.
- (b) **The current attempt is checked on every delivery.** The instruction endpoint returns 200 only if attempt n is the payout's current attempt and is `requested` or `indeterminate`; otherwise 409 `attempt_not_current`, and the worker ACKs without calling the bank. The worker uses the instruction's amount, never the event's; a mismatch is poison and goes to the DLQ. The `disbursements(payout_id, attempt)` unique row records each try.
- (c) **Results apply only to the current attempt.** payroll applies a result with a conditional update on `payout_attempts(payout_id, attempt)`, only while the attempt is current and `requested`/`indeterminate`.
  - A stale attempt gets 409 `attempt_not_current`.
  - A current attempt that's already final gets 200 with its stored outcome, and nothing changes.
  - The result Idempotency-Key is `<event_id>:<outcome>`, so a stored `indeterminate` response never masks a later `settled` on the same event.
- (d) **An uncertain outcome is never final.** If the bank can't be reached within `PAYOUT_MAX_DELIVERIES`, including a timeout after the bank may have settled, the worker reports `indeterminate`, **not** `failed`. An indeterminate attempt can't be retried. payroll's maintenance task re-publishes it until the bank answers `settled` or `rejected`:
  - same attempt, so the same bank key;
  - a **fresh `event_id`** each time;
  - exponential backoff from `PAYOUT_REPUBLISH_AFTER_S` (360 s), capped at 900 s;
  - tracked by `last_published_at` and `publish_count`.

  The worker's gate is the instruction check, never `processed_events` or `disbursements`.
  - After `PAYOUT_REPUBLISH_MAX` (10) re-publishes, or a `poison` report, the attempt becomes `needs_operator`: re-publishing stops and an alert pages.
  - **Key retention:** providers expire idempotency keys. For an attempt older than `BANK_KEY_SAFE_WINDOW_S` (bank-stub: 86400; a real adapter must state its retention), the worker only calls `GET /v1/transfers/{key}`. If found, it uses that transfer; if not found, the attempt becomes `needs_operator`. It never POSTs after the window.
- (e) **Only a definite rejection opens a retry.** A retry (attempt n+1, new key) is allowed only after the bank has **definitely rejected** attempt n.
  - A definite rejection is **only** HTTP 2xx with body `status: rejected` and a `reason`.
  - Everything else is "no answer" and goes down the indeterminate path: timeouts, 5xx, 429, 409 (key in progress), 401/403, 404, 422 and malformed bodies. This overrides the resilient client's generic `ProviderRejected(4xx)` mapping for this provider.
- *Considered and not needed for 0.1.0:* a void endpoint (`POST /v1/transfers/{key}/void`) to give up on an indeterminate attempt. Re-driving with the same key is safe without it. A real provider adapter may add it, so operators can cancel during a long outage.
**Stream loss:** the same re-publish covers lost stream entries (Redis failover, trim, FLUSHALL), because payroll's `payout_attempts` table is the system of record.
Recommendation: option 1 with guards (a)–(e).
Decision: the recommendation above, accepted by the user on 2026-11-10 (plan sign-off).
Consequences:
- Worker → payroll synchronous calls need a breaker and a retry budget.
- If payroll is down for longer than the budget, messages go to the DLQ (alert `PayoutDLQ`), and the re-publish redrives them after recovery (`payroll_payouts_stuck` raises `PayoutsStuck` meanwhile).
- During a long bank outage, runs stay `processing` ("awaiting bank") rather than failing.
- The internal endpoints accept only `iss=payout-worker` tokens signed with the worker's issuer-bound key, and scoped to the token's `cid`.
- Tests: a hypothesis stateful model of the interleavings, plus the integration and resilience rows in plan §12.1.
