# Runbook: PayoutNeedsOperator

| | |
|---|---|
| **Severity** | page |
| **Service** | payroll |
| **Alert** | `max(payroll_attempts_needs_operator) > 0` for 1 m (`deploy/observability/prometheus/rules/alerts.yml`) |
| **Requirement** | PAY-409 (poison and needs_operator), PAY-502 (operator resolution, R8), PAY-NFR-11; the C48 wait |
| **Dashboard** | Queues and workers, `http://127.0.0.1:4101/d/payrun-queues` |

## What it means

At least one payout attempt is in `needs_operator`: the system cannot safely decide whether the money moved, so it stopped and waits for a person. An attempt gets there when:

- the worker reported it as **poison** (`schema_invalid`, `amount_mismatch`, `key_window_expired`): `payout.poison_reported` in the worker, `payout.needs_operator` in payroll;
- re-publish reached `PAYOUT_REPUBLISH_MAX` (10) without a final answer;
- the relay refused the envelope (`envelope_invalid`, C71). The refused outbox row stays unsent and keeps OutboxStuck firing: after resolving the attempt here, delete that row as [OutboxStuck](OutboxStuck.md) describes.

The gauge counts attempts with status `needs_operator`, computed at metric collection; `max()` collapses replicas.

## Impact

The payout is blocked: the employee is not paid, and the run cannot finish. The UI shows the payout as "blocked".

## First checks

1. **How many** (Queues dashboard, "Needs operator"):

   ```promql
   max(payroll_attempts_needs_operator)
   ```

2. **Which attempts, and why** (Loki):

   ```logql
   {service_name="payroll"} | event="payout.needs_operator"
   ```

   ```logql
   {service_name="payout-worker"} | event=~"payout.poison_reported|payout.key_window_expired"
   ```

   Note `payout_id`, `pay_run_id`, `attempt` and the reason. In the web app the run page lists the blocked payouts with their history.
3. **Ask the bank first (R8).** The attempt's bank key is `<payout_id>:<attempt>`:

   ```sh
   # dev and CI (test profile): bank-stub is published on 127.0.0.1:$BANK_STUB_PORT; elsewhere use BANK_BASE_URL
   curl -fsS -H "X-Api-Key: $BANK_STUB_API_KEY" "http://127.0.0.1:$BANK_STUB_PORT/v1/transfers/<payout_id>:<attempt>"
   ```

   The API key is a secret: never paste it into tickets. Write down the transfer's status and reference, or "not found".

   **"Not found" is not proof after `key_window_expired` (C73).** Once the attempt is older than `BANK_KEY_SAFE_WINDOW_S` (24 h), the bank may have dropped its idempotency record, so a key lookup can say "not found" for a transfer that settled. For these attempts, reconcile against the **bank statement** (amount, account last 4, date) and record the statement reference in the resolve note.
4. **Related alerts:** PayoutDLQ (poison messages are dead-lettered too).

## Likely causes

- A poison event (schema, amount or ID mismatch): a producer bug, or a hand-written stream entry.
- The bank key window expired (`BANK_KEY_SAFE_WINDOW_S`, 24 h) before the worker could look the transfer up: the worker may only GET by key, never POST again.
- The bank never answered across 10 re-publishes.

## Mitigation

Resolve each attempt in the web app (run page → payout → **Resolve**, approver role) or with `POST /v1/pay-runs/{run_id}/payouts/{payout_id}/resolve {"action": ..., "note": ...}`:

1. **The bank shows a settled transfer:** choose **Send again** (`redrive`). The worker re-sends with the same key, the bank answers with the original transfer, and the payout becomes `paid` with that reference. Never mark it rejected.
2. **The bank shows a rejected transfer:** choose **Send again**; the bank's stored answer is applied and the payout becomes `failed`, so the normal retry can follow after the bank details are fixed.
3. **The bank shows no transfer, and the cause is fixed** (for example the poison came from a bug now fixed): choose **Send again**.
4. **The bank shows no transfer and the attempt must not be sent:** choose **Mark rejected** (`mark_rejected`). Payroll refuses it with 409 `resolve_too_early` until `PAYOUT_MARK_REJECTED_MIN_WAIT_S` (120 s) after the attempt entered `needs_operator` (C48). The payout becomes `failed` and the approver can retry it later.

**Send again does not help once the attempt is older than 24 h**, whatever its reason (`republish_limit`, `envelope_invalid`, `schema_invalid`, ...). A redrive keeps the attempt's `requested_at`, and the worker only looks a key up (never POSTs) once `now − requested_at` exceeds `BANK_KEY_SAFE_WINDOW_S` (24 h, a worker setting payroll doesn't know). So the redrive comes straight back as `key_window_expired`, one cycle later. Check the attempt's age first: the run page's payout history shows each attempt's requested time, as does `requested_at` in `GET /v1/pay-runs/{run_id}/payouts`. Past 24 h, go straight to the statement check and **Mark rejected**.

Mark rejected without the bank check risks a double payment: if the bank later settles the attempt, PayoutLateSettlement pages. After `key_window_expired` nothing asks the bank about the old key again, so a missed settled transfer would **not** page: only the statement check above protects against it. For the same reason, **Send again** cannot help an expired-key attempt (the worker may only look it up, and the lookup keeps saying "not found"); payroll refuses it, and the only resolution is **Mark rejected** after the statement check.

With dual approval on, the person who marks an attempt rejected cannot retry the payout while that attempt is current (C73): a second approver checks the statement reference in the note before retrying. Only an operator's **Mark rejected** counts: someone who chose **Send again** and then saw the bank reject the transfer (for example `account_closed`) may retry it.

**Choose who marks it rejected.** Retry also refuses anyone who changed the employee's bank details after the payout's snapshot, until a second person has released those details (rule `bank_editor`). So if approver A fixed the bank details and approver B marks the attempt rejected, A gets 403 `bank_editor` and B gets 403 `resolver`: nobody can retry. To avoid that, **the person who changed the bank details after the snapshot does the Mark rejected**, and the other approver retries.

If both are already stuck, a second person re-confirms the bank details: anyone other than A with the `payroll_admin` role (B included) checks the details with the employee and saves them again on the employee page (`PATCH /v1/employees/{id}` with `bank_routing` and `bank_account`). That person is now the bank editor, so A is neither the editor nor the resolver and can retry. Both refusals are audited (`payout.retry_denied`, with the rule).

## Confirm recovery

- The gauge returns to 0 and the alert resolves:

  ```promql
  max(payroll_attempts_needs_operator)
  ```

- Each resolution is audited (`payout.operator_resolved`):

  ```logql
  {service_name="payroll"} | event="payout.operator_resolved"
  ```
