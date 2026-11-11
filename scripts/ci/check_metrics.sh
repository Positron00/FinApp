#!/usr/bin/env bash
# Required metric series are in Prometheus (platform LLD §3.15, §3.3; PAY-NFR-10; C65).
#
# Usage: scripts/ci/check_metrics.sh      (against the running stack; e2e.sh runs it last)
#
# Queries /api/v1/series on 127.0.0.1:$PROMETHEUS_PORT for each entry below and fails listing the
# missing ones. An entry is a metric name, or a full series selector when it contains `{` (a
# pre-initialised label set an alert reads, which the bare name would not prove). This guards the
# metric-naming rule (unit suffixes and _total added by the Collector's Prometheus exporter): a
# renamed instrument breaks dashboards and alerts silently, so it breaks CI instead. Waits up to CHECK_METRICS_WAIT_S (default 90 s) for late series, since
# the SDKs export every 15 s and Prometheus scrapes every 15 s.
# shellcheck source=scripts/ci/lib.sh disable=SC1091  # followed with -x; single-file runs (pre-commit) cannot
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need curl python3

REQUIRED=(
  http_server_request_duration_seconds_count # every Python service (OTel semconv)
  bff_request_duration_seconds_count         # web
  queue_messages_total                       # worker, pre-initialised (C21)
  queue_consumer_lag                         # worker
  circuit_breaker_state                      # pycommon http.Guard and the web
  external_requests_total                    # pycommon ResilientClient
  payroll_payout_settlement_seconds_count    # payroll (SETTLEMENT_BUCKETS)
  identity_emails_pending                    # identity
  rate_limiter_errors_total                  # pycommon RateLimiter, pre-initialised
  # the web limiter's series, pre-initialised at server start (RateLimiterErrors; C72, C77)
  'rate_limiter_errors_total{service_name="web",component="web"}'
  redis_stream_length                        # redis_exporter --check-single-streams
  pg_up                                      # postgres-exporter
  otelcol_exporter_sent_spans                # Collector self-metrics: 0.164 has no _total (C65)
  outbox_unsent                              # payroll collection-time gauges (F4a; R26, C18)
  outbox_oldest_age_seconds
  payroll_payouts_stuck
  payroll_runs_processing_oldest_age_seconds
  payroll_attempts_needs_operator
  payroll_late_settlements_total             # pre-initialised to 0 (PayoutLateSettlement, C71)
  identity_emails_failed_total               # identity, pre-initialised (EmailOutboxStuck)
  identity_email_oldest_pending_age_seconds  # identity collection-time gauge (EmailOutboxStuck)
  redis_memory_used_bytes                    # redis_exporter (RedisMemoryHigh)
  redis_memory_max_bytes
  queue_dlq_messages                         # worker collection-time gauge (Queues, PayoutDLQ drill)
  process_memory_usage_bytes                 # Python services' RSS (nightly soak resources gate)
  # identity pre-initialises every outcome at start, so this exists before any sign-in
  # (SigninFailuresSpike; C77)
  'auth_signins_total{service_name="identity",outcome="failure"}'
  external_request_duration_seconds_count    # pycommon ResilientClient histogram (Bank dashboard)
)

port="${PROMETHEUS_PORT:-$(env_val PROMETHEUS_PORT)}"
[[ -n "$port" ]] || die "PROMETHEUS_PORT is not set and not in .env (run scripts/dev-env.sh)"
url="http://127.0.0.1:$port"

present() { # present ENTRY -> 0 when Prometheus has at least one series for ENTRY in the last hour
  local start match="$1"
  [[ "$match" == *"{"* ]] || match="{__name__=\"$1\"}"
  start="$(($(date +%s) - 3600))"
  curl -fsS --max-time 5 -G "$url/api/v1/series" --data-urlencode "match[]=$match" \
    --data-urlencode "start=$start" |
    python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin)["data"] else 1)'
}

curl -fsS --max-time 5 "$url/-/ready" >/dev/null || die "Prometheus is not ready at $url"

deadline=$(($(date +%s) + ${CHECK_METRICS_WAIT_S:-90}))
missing=("${REQUIRED[@]}")
while :; do
  still=()
  for name in "${missing[@]}"; do
    present "$name" || still+=("$name")
  done
  if ((${#still[@]} == 0)); then
    missing=()
    break
  fi
  missing=("${still[@]}")
  (($(date +%s) < deadline)) || break
  log "waiting for ${#missing[@]} series: ${missing[*]}"
  sleep 10
done

if ((${#missing[@]} > 0)); then
  log "missing required series:"
  printf '  %s\n' "${missing[@]}" >&2
  exit 1
fi
log "all ${#REQUIRED[@]} required series present"
