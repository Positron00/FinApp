# Runbook: BankErrorRateHigh

| | |
|---|---|
| **Severity** | warning (ticket) |
| **Service** | payout-worker (its bank client) |
| **Alert** | `provider:external_errors:ratio_rate5m{provider="bank"} > 0.05` for 10 m (`deploy/observability/prometheus/rules/alerts.yml`) |
| **Requirement** | PAY-404; C55 (outcome labels) |
| **Dashboard** | Bank (stub) dependency, `http://127.0.0.1:4101/d/payrun-bank` |

## What it means

More than 5% of the worker's bank call attempts failed at the transport or HTTP level for 10 minutes. The ratio counts every `external_requests_total{provider="bank"}` outcome except `ok`: `client_error`, `rate_limited`, `server_error`, `timeout`, `connect_error`, `breaker_open`. A **business rejection** (account closed, insufficient funds) is an HTTP 2xx answer and counts as `ok`; those are in `payouts_bank_transfers_total{outcome="rejected"}`.

## Impact

Retries slow payouts down; failed calls end attempts `indeterminate`, so they wait for re-publish. If it gets worse the breaker opens (BankBreakerOpen).

## First checks

1. **Which outcomes and operations:**

   ```promql
   sum by (outcome, operation) (rate(external_requests_total{provider="bank", outcome!="ok"}[5m]))
   ```

2. **Latency** (timeouts follow slow answers):

   ```promql
   histogram_quantile(0.95, sum by (le) (rate(external_request_duration_seconds_bucket{provider="bank"}[5m])))
   ```

3. **Worker logs** (Loki), with trace links to the failing calls:

   ```logql
   {service_name="payout-worker"} | event=~"external.request.failed|external.request.retry|bank.transfer.no_answer"
   ```

4. **The bank side** (dev, uat): bank-stub's 5xx on the Bank dashboard and its log `{service_name="bank-stub"}`.

## Likely causes

- `timeout`: the bank is slow (deadline `BANK_TOTAL_DEADLINE_S`, 15 s; read timeout per attempt).
- `server_error` / `rate_limited`: the bank is degraded or throttling us (429).
- `client_error`: a 401 (rotated API key), 409 or 422 from the bank. These count as "no answer" and never fail a payout; look for a contract change.
- `connect_error`: network or DNS trouble.

## Mitigation

1. For `client_error` 401, fix the `BANK_STUB_API_KEY` secret and restart the worker.
2. For throttling, lower `PAYOUT_CONCURRENCY` or worker replicas until the bank recovers.
3. For bank-side failures, contact the bank; payouts recover on their own through re-publish.

## Confirm recovery

- The ratio stays below 5% and the alert resolves:

  ```promql
  provider:external_errors:ratio_rate5m{provider="bank"}
  ```
