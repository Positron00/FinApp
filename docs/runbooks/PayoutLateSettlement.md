# Runbook: PayoutLateSettlement

| | |
|---|---|
| **Severity** | page (possible double payment) |
| **Service** | payroll |
| **Alert** | `increase(payroll_late_settlements_total[15m]) > 0 or (payroll_late_settlements_total > 0 unless max_over_time(payroll_late_settlements_total[1h] offset 15m)) or (payroll_late_settlements_total - max_over_time(payroll_late_settlements_total[1h] offset 15m) > 0)` (`deploy/observability/prometheus/rules/alerts.yml`) |
| **Requirement** | PAY-NFR-02 (a paid payout has exactly one settled transfer); C71 |
| **Design** | `docs/specs/payrun-0.1/design-low-level-payroll.md` §4.7 "Late settlement" |
| **Dashboard** | Queues and workers, `http://127.0.0.1:4101/d/payrun-queues` ("Late settlements") |

## What it means

The bank answered **settled** for a payout attempt that payroll had already stopped treating as payable:

- **Same attempt, already rejected.** The usual cause is an operator who used **Mark rejected** on a `needs_operator` attempt (`failure_reason = operator_rejected`) without first finding the transfer at the bank. The bank had in fact paid it.
- **Earlier attempt, already replaced.** A retry has since opened a later attempt. If that later attempt is `settled` too, **the employee has very likely been paid twice**.

Payroll does not change the payout's state. The worker still gets the normal answer: 200 with the stored outcome (`rejected`) for the current attempt, or 409 `attempt_not_current` for an earlier one. For each payout and attempt, payroll records the event once:

- an audit row `payout.late_settlement` (actor `service:payout-worker`) whose `details` hold:
  - `attempt`, `current_attempt`, `attempt_status` and `failure_reason`;
  - `bank_reference`, the bank's reference for the late transfer;
  - `later_attempt_settled`, plus `later_settled_attempt` when it is true;
- an error log `payout.late_settlement` with the same fields plus `payout_id`, `pay_run_id` and `recorded` (`false` when the same answer arrived again and was not counted twice);
- one increment of `payroll_late_settlements_total` (pre-initialised at 0, so the first one fires).

A restarted payroll that records a late settlement before its first metric export shows up as a new series that starts at 1, which `increase()` reads as 0; the rule's second arm (C75) pages on a series above 0 that had no sample in the hour before the window, so that case pages too, and a data gap on an old series does not. The third arm (C77) pages when an old series rose during a data gap longer than the window (`increase()` sees only the post-gap samples); `$value` is the rise. A gap longer than an hour makes an old series look new, so it may page with its full value: check the audit rows below. The alert clears 15 minutes after the last increment. **Clearing does not mean the problem is resolved.**

## Impact

An employee may have been paid twice, or was paid while payroll shows the payout as failed. Either way the books and the bank disagree until someone acts.

## First checks

1. **Find the event.** In Grafana Explore (Loki):

   ```logql
   {service_name="payroll"} | event="payout.late_settlement"
   ```

   Or, as an admin, call `GET /v1/audit-events?event=payout.late_settlement`. The web Audit page lists payroll events too, but can't filter by event (C69), so look for `payout.late_settlement` rows by hand. Note `payout_id`, `pay_run_id`, `attempt`, `bank_reference`, `later_attempt_settled` and `later_settled_attempt`.
2. **How many in the window** (Queues dashboard, "Late settlements"; per series, the rise over the window, across a data gap too, or a new series's full value):

   ```promql
   sum(clamp_min(last_over_time(payroll_late_settlements_total[15m]) - max_over_time(payroll_late_settlements_total[1h] offset 15m), 0) or (last_over_time(payroll_late_settlements_total[15m]) unless max_over_time(payroll_late_settlements_total[1h] offset 15m))) or vector(0)
   ```

3. **Look at the payout.** Call `GET /v1/pay-runs/{pay_run_id}/payouts` and find the payout. Note each attempt's status and `bank_reference`, and the payout status.
4. **Ask the bank about every attempt.** The bank key of attempt *n* is `<payout_id>:<n>`. For the late attempt and for every later one, call `GET {BANK_BASE_URL}/v1/transfers/<payout_id>:<n>` with the `X-Api-Key` header; the key is a secret, so never paste it into tickets. Write down each transfer's status, amount and reference.
   - **Two settled transfers:** a double payment (most often `later_attempt_settled = true`).
   - **One settled transfer (the late one), with the payout `failed` or a later attempt not settled:** the employee **was paid**, but payroll shows the payout as not paid.
5. **Check the payroll audit trail** for the payout: `GET /v1/audit-events?entity_type=payout&entity_id=<payout_id>`. The `payout.operator_resolved` row shows who used Mark rejected and when. The note text is not stored; its length is.
6. **Check the worker** (optional). In payout-worker's `disbursements` row for `(payout_id, attempt)` (`scripts/stack.sh psql payouts_app`), `outcome` and `bank_reference` show when the bank's answer was first recorded. A long gap before the result post points at the DLQ or a payroll outage (see PayoutDLQ).

## Likely causes

- **Mark rejected without the bank check** (R8): the attempt was in flight or already settled at the bank.
- **A result that reached payroll late:** the worker's result post sat in the DLQ, or payroll was down, while the approver retried the payout and opened a later attempt.

## Mitigation

1. **Stop further money movement for this payout.**
   - If the payout is `failed` and its late attempt is settled, **do not retry it**: a retry would pay a third time, or a second time if no later attempt settled.
   - If a later attempt is still `requested` or `needs_operator`, check it at the bank before you resolve it.
2. **Double payment:** contact the bank's operations desk. Ask them to recall or reverse the duplicate transfer, quoting both bank references. If the bank can't recall it, start the employee overpayment-recovery process with HR and finance.
3. **Paid but shown as failed:** tell payroll operations that the employee was paid, with the bank reference, so nobody pays them by hand or retries the payout.
4. **Record it.** Open an incident ticket with the payout ID, the run ID, the attempts and their bank references, what the bank confirmed, and the recall outcome. Link it from the run's notes. The audit row stays as the system record.
5. **Fix the cause.** If an operator used **Mark rejected** without the bank check, review the PayoutNeedsOperator runbook with them. Mark rejected is only safe after `GET /v1/transfers/<payout_id>:<attempt>` shows **no** transfer (after `key_window_expired`, 'not found' is not proof: check the bank statement and put its reference in the note; C73).

## Confirm recovery

- The alert resolves 15 minutes after the last increment; that only means no **new** late settlement arrived:

  ```promql
  sum(clamp_min(last_over_time(payroll_late_settlements_total[15m]) - max_over_time(payroll_late_settlements_total[1h] offset 15m), 0) or (last_over_time(payroll_late_settlements_total[15m]) unless max_over_time(payroll_late_settlements_total[1h] offset 15m))) or vector(0)
  ```

- Recovery is the incident ticket closed: each `payout.late_settlement` audit row has a bank confirmation and a recall or record outcome.

## Related

- PayoutNeedsOperator: how to choose between Send again and Mark rejected.
- PayoutDLQ: result posts that never reached payroll, the usual reason the answer was late.
