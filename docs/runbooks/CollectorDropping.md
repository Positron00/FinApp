# Runbook: CollectorDropping

| | |
|---|---|
| **Severity** | warning (ticket) |
| **Service** | otel-collector |
| **Alert** | the summed rate of `otelcol_exporter_send_failed_{spans,metric_points,log_records}` and `otelcol_receiver_refused_spans` (with or without `_total`) `> 0` for 10 m (`deploy/observability/prometheus/rules/alerts.yml`) |
| **Requirement** | PAY-NFR-09, PAY-NFR-10; C65 (Collector 0.164 self-metrics have no `_total`) |
| **Dashboard** | Platform, `http://127.0.0.1:4101/d/payrun-platform` ("OTel Collector") |

## What it means

For 10 minutes the Collector has failed to export telemetry to a backend (Tempo for traces, Prometheus scrape for metrics, Loki for logs), or refused spans from the apps (usually the `memory_limiter`). The apps never block on telemetry: they drop what the Collector does not take.

## Impact

Gaps in traces, logs or metrics. Dashboards may show holes and alerts based on missing data may stay quiet. The app itself is not affected.

## First checks

1. **Which signal and which side:**

   ```promql
   sum by (exporter) (rate({__name__=~"otelcol_exporter_send_failed_spans(_total)?"}[5m]))
   ```

   ```promql
   sum by (exporter) (rate({__name__=~"otelcol_exporter_send_failed_log_records(_total)?"}[5m]))
   ```

   ```promql
   sum by (receiver) (rate({__name__=~"otelcol_receiver_refused_spans(_total)?"}[5m]))
   ```

2. **The backends are scraped and up:**

   ```promql
   up{job=~"tempo|loki|prometheus|otelcol"}
   ```

3. **The Collector's own log** (it writes to stdout, not Loki): `scripts/stack.sh logs otel-collector`; look for `Exporting failed` and `memory_limiter` messages.

**Known limit (C77):** if the unsuffixed and `_total` counters exist at the same time with the same labels (for example during a Collector upgrade), the rule's `{__name__=~"…(_total)?"}` match fails with "vector cannot contain metrics with the same labelset". That is a rule evaluation error (visible in Prometheus → Alerts), not a page; query each name on its own until the old series expire.

## Likely causes

- Tempo or Loki is down, restarting or out of disk.
- The Collector hits its `memory_limiter` (80% of its memory limit) during a burst: refused spans.
- A config change to an exporter endpoint.

## Mitigation

1. Restart or fix the failing backend (`docker compose ... restart tempo` or `loki`); check its disk.
2. For memory pressure, give the Collector more memory or reduce the batch size; in prod, tail sampling keeps volume down.
3. Validate any changed config with `otelcol-contrib validate --config` (as `scripts/ci/manifests.sh` does).

## Confirm recovery

- All failure rates return to 0, and new traces and logs appear in Grafana Explore:

  ```promql
  sum(rate({__name__=~"otelcol_exporter_sent_spans(_total)?"}[5m]))
  ```
