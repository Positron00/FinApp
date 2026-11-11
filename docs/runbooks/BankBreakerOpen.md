# Runbook: BankBreakerOpen

| | |
|---|---|
| **Severity** | warning (ticket) |
| **Service** | payout-worker (its bank client) |
| **Alert** | `max(circuit_breaker_state{target="bank"}) == 2` for 5 m (`deploy/observability/prometheus/rules/alerts.yml`) |
| **Requirement** | PAY-404 (bank calls with deadline, retries and breaker) |
| **Dashboard** | Bank (stub) dependency, `http://127.0.0.1:4101/d/payrun-bank` |

## What it means

payout-worker's circuit breaker for the bank has been **open** (2) for 5 minutes. It opens after `BREAKER_FAILURES` (5) consecutive failed calls and tries again (half-open, 1) after `BREAKER_RESET_S` (30 s); staying open means every probe fails too. While open, bank calls fail fast with `outcome="breaker_open"`, and attempts end `indeterminate` (never `failed`) and wait for payroll's re-publish.

## Impact

No payouts are being sent to or confirmed by the bank. Runs stay `processing` (PayoutSettlementSlow pages). Nothing is paid twice: re-publish reuses each attempt's bank key.

## First checks

1. **Breaker and outcomes** (Bank dashboard):

   ```promql
   max by (target) (circuit_breaker_state{service_name="payout-worker", target="bank"})
   ```

   ```promql
   sum by (outcome) (rate(external_requests_total{provider="bank"}[5m]))
   ```

   `connect_error` or `timeout` before the breaker opened points at the network or the bank being down; `server_error` at the bank failing.
2. **Worker logs** (Loki):

   ```logql
   {service_name="payout-worker"} | event=~"external.breaker.opened|external.breaker.half_open|external.request.failed|bank.transfer.no_answer"
   ```

3. **The bank itself.** In dev and uat, bank-stub: `scripts/stack.sh ps`, its `/readyz`, and its logs (`{service_name="bank-stub"}`). In prod, the bank's status page and our network path to it.
4. **In tests:** a Toxiproxy toxic or a disabled `bank-stub` proxy (`curl http://127.0.0.1:$TOXIPROXY_PORT/proxies`).

## Likely causes

- The bank (or bank-stub) is down or restarting.
- Network, DNS or TLS failure between the worker and the bank.
- The bank rejects our API key (401): counted as `client_error`, so check for a rotated `BANK_STUB_API_KEY`.

## Mitigation

1. Restore the bank connection (start bank-stub; fix the network path; fix the API key secret and restart the worker).
2. Do not retry payouts by hand. Once the breaker closes, payroll's re-publish redrives every `indeterminate` attempt with the same key.

## Confirm recovery

- The breaker returns to 0 (closed) and `ok` outcomes resume:

  ```promql
  sum by (outcome) (rate(external_requests_total{provider="bank"}[5m]))
  ```

- `external.breaker.closed` in the worker's log, and `payroll_runs_processing_oldest_age_seconds` falls back to 0 as runs finish.
