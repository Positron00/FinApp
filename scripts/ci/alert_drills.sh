#!/usr/bin/env bash
# Alert drills (H2; platform LLD §8.4-8.5; PAY-NFR-10, PAY-NFR-11; ADR 0010).
#
# Usage: scripts/ci/alert_drills.sh [AlertName ...]        (default: every paging alert, in the
#        order below)
#
# Needs the running Compose stack with the core, obs and test profiles (scripts/stack.sh up), seeded
# (scripts/stack.sh seed): the drills create and approve pay runs for the seeded monthly employees.
# For each paging alert it:
#   1. waits until the alert is not firing in Prometheus (a notification for an alert that is
#      already firing would not be sent again within the drill), then clears alert-sink;
#   2. causes it on the stack:
#        PayoutDLQ               a poison stream entry (unparseable envelope) XADDed as the payroll
#                                Redis user: the worker dead-letters it at once
#        PayoutNeedsOperator     payout-worker stopped, a run approved, then a poison result for one
#                                attempt posted to payroll with a worker token from mint_token
#        PayoutLateSettlement    payout-worker stopped, a run approved, one attempt reported
#                                rejected and then settled (forced late settlement, mint_token)
#        PayoutSettlementSlow    Toxiproxy's bank-stub proxy disabled, a run approved
#        PayoutsStuck            payout-worker stopped, a run approved (PAYOUT_STUCK_AFTER_S 600 s
#                                + for 10 m: this drill takes about 22 minutes)
#        BffAvailabilityFastBurn identity stopped, sign-in requests sent to the BFF (503s)
#   3. waits until Alertmanager delivers a firing notification for it to alert-sink /page;
#   4. checks that the dashboard query shows the cause (a Prometheus instant query from the
#      alert's dashboard), and prints the runbook path (and checks the runbook_url annotation);
#   5. resets the fault (and waits, bounded, for the cause to clear; a slow clear is a warning).
# Every fault is also undone on exit (Toxiproxy reset, identity and payout-worker started, load
# stopped). A drill that fails is reported and the others still run; the exit status is 1 if any
# drill failed. The runs and payouts the drills create stay in the dev database (the late-settlement
# drill leaves one payout failed, as the runbook describes).
#
# Env (seconds): DRILL_TIMEOUT_S (alert delivery, default 900), DRILL_STUCK_TIMEOUT_S (PayoutsStuck,
# 1800), DRILL_QUIET_WAIT_S (step 1, 1200), DRILL_RECOVERY_WAIT_S (step 5, 900), DRILL_POLL_S (10),
# DRILL_LOAD_WORKERS (BFF load loops, 4).
#
# Bash 3.2 compatible; needs docker, curl, python3 and uv (for mint_token). Not run in CI: the lead
# runs it on the stack after H1 (it stops containers and changes Toxiproxy).
# shellcheck source=scripts/ci/lib.sh disable=SC1091  # followed with -x; single-file runs (pre-commit) cannot
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need curl docker python3 uv

PAGING_ALERTS=(PayoutDLQ PayoutNeedsOperator PayoutLateSettlement PayoutSettlementSlow PayoutsStuck
  BffAvailabilityFastBurn)

DRILL_TIMEOUT_S="${DRILL_TIMEOUT_S:-900}"
DRILL_STUCK_TIMEOUT_S="${DRILL_STUCK_TIMEOUT_S:-1800}"
DRILL_QUIET_WAIT_S="${DRILL_QUIET_WAIT_S:-1200}"
DRILL_RECOVERY_WAIT_S="${DRILL_RECOVERY_WAIT_S:-900}"
DRILL_POLL_S="${DRILL_POLL_S:-10}"
DRILL_LOAD_WORKERS="${DRILL_LOAD_WORKERS:-4}"

port() {
  local v
  v="$(env_val "$1")"
  [[ -n "$v" ]] || die "$1 is not in ${ENV_FILE:-.env} (run scripts/dev-env.sh and scripts/stack.sh up)"
  printf '%s' "$v"
}
PROM="http://127.0.0.1:$(port PROMETHEUS_PORT)"
SINK="http://127.0.0.1:$(port ALERT_SINK_PORT)"
ALERTMANAGER="http://127.0.0.1:$(port ALERTMANAGER_PORT)"
TOXI="http://127.0.0.1:$(port TOXIPROXY_PORT)"
WEB="http://127.0.0.1:$(port WEB_PORT)"
PAYROLL="http://127.0.0.1:$(port PAYROLL_PORT)"

LOAD_PIDS=()

# ---------------------------------------------------------------- helpers
# wait_until TIMEOUT_S DESCRIPTION CMD... -> 0 once CMD succeeds, 1 after TIMEOUT_S
wait_until() {
  local timeout="$1" what="$2" deadline
  shift 2
  deadline=$(($(date +%s) + timeout))
  log "waiting up to ${timeout}s: $what"
  until "$@"; do
    if (($(date +%s) >= deadline)); then
      log "timed out after ${timeout}s: $what"
      return 1
    fi
    sleep "$DRILL_POLL_S"
  done
  log "done: $what"
}

# prom_value EXPR -> the first sample's value, "none" for an empty result
prom_value() {
  curl -fsS --max-time 10 -G "$PROM/api/v1/query" --data-urlencode "query=$1" |
    python3 -c 'import json,sys; r=json.load(sys.stdin)["data"]["result"]; print(r[0]["value"][1] if r else "none")'
}

# prom_has EXPR -> 0 when EXPR returns at least one sample (put the comparison in EXPR)
prom_has() {
  local v
  v="$(prom_value "$1")" || return 1
  [[ "$v" != none ]]
}

prom_lacks() { ! prom_has "$1"; }

# check_query DESCRIPTION EXPR -> logs the value; 1 when EXPR is empty
check_query() {
  local v
  v="$(prom_value "$2" || echo error)"
  if [[ "$v" == none || "$v" == error ]]; then
    log "dashboard check FAILED ($1): $2 -> $v"
    return 1
  fi
  log "dashboard check ok ($1): $v  <- $2"
}

alert_quiet() { prom_lacks "ALERTS{alertname=\"$1\",alertstate=\"firing\"}"; }

sink_clear() { curl -fsS --max-time 5 -X DELETE "$SINK/all" >/dev/null; }

# sink_runbook ALERT -> prints the runbook_url of a firing ALERT delivered to /page; 1 if none yet
sink_runbook() {
  curl -fsS --max-time 5 "$SINK/page" | python3 -c '
import json, sys
name = sys.argv[1]
for item in json.load(sys.stdin):
    payload = item.get("payload") or {}
    for alert in payload.get("alerts", []):
        if alert.get("status") == "firing" and alert.get("labels", {}).get("alertname") == name:
            print(alert.get("annotations", {}).get("runbook_url", ""))
            sys.exit(0)
sys.exit(1)
' "$1"
}

sink_has() { sink_runbook "$1" >/dev/null; }

# delivered ALERT TIMEOUT -> waits for the page, then checks the runbook annotation and file
delivered() {
  local alert="$1" url file="docs/runbooks/$1.md"
  wait_until "$2" "Alertmanager delivers $alert to alert-sink /page" sink_has "$alert" || return 1
  url="$(sink_runbook "$alert")"
  log "delivered: $alert (runbook_url $url)"
  [[ "$url" == */docs/runbooks/"$alert".md ]] || {
    log "runbook_url does not point at $file"
    return 1
  }
  [[ -f "$file" ]] || {
    log "missing runbook $file"
    return 1
  }
  log "runbook: $file"
}

service_up() { compose up -d --no-deps --wait --wait-timeout 180 "$1" >/dev/null; }
service_stop() { compose stop "$1" >/dev/null; }

redis_cli() { # redis_cli USER PASSWORD_KEY ARGS... (inside the redis container; no password in argv)
  local user="$1" key="$2"
  shift 2
  compose exec -T -e REDISCLI_AUTH="$(env_val "$key")" redis redis-cli --no-auth-warning --user "$user" "$@"
}

toxi_reset() { curl -fsS --max-time 5 -X POST "$TOXI/reset" >/dev/null; }

stop_load() {
  local pid
  for pid in ${LOAD_PIDS[@]+"${LOAD_PIDS[@]}"}; do kill "$pid" 2>/dev/null || true; done
  for pid in ${LOAD_PIDS[@]+"${LOAD_PIDS[@]}"}; do wait "$pid" 2>/dev/null || true; done
  LOAD_PIDS=()
}

DRILLS_STARTED=0
safety_reset() {
  ((DRILLS_STARTED)) || return 0 # never start containers when the precondition checks failed
  stop_load
  toxi_reset || log "Toxiproxy reset failed"
  service_up identity || log "could not start identity"
  service_up payout-worker || log "could not start payout-worker"
}
on_exit safety_reset

# payroll_api CMD ARGS... -> payroll calls with mint_token tokens (dev only; tokens live 60 s).
#   new-run                         create and calculate a monthly run for a free period -> run id
#   approve RUN                     approve it (a second identity, so dual approval passes)
#   pick-attempt RUN                -> "<payout_id> <attempt>" of a payout whose attempt is requested
#   result PAYOUT ATTEMPT OUTCOME [REASON|BANK_REFERENCE]   post a result as payout-worker
#   resolve RUN PAYOUT ACTION       redrive or mark_rejected as the approver
#   run-status RUN                  -> the run's status
payroll_api() {
  PAYROLL_URL="$PAYROLL" uv run --quiet python - "$@" <<'PY'
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path.cwd()))
from scripts.mint_token import load_env, mint  # noqa: E402

ENV = load_env()
BASE = os.environ["PAYROLL_URL"]
# Fixed drill identities: payroll trusts the internal JWT, these are not identity users.
PREPARER = "0193a000-0000-7000-8000-00000000d201"
APPROVER = "0193a000-0000-7000-8000-00000000d202"


def user(sub, roles):
    return {"Authorization": "Bearer " + mint("payroll", sub, roles, name="Alert drill", env=ENV)}


def worker():
    tok = mint("payroll", "service:payout-worker", [], issuer="payout-worker", env=ENV)
    return {"Authorization": "Bearer " + tok}


def call(method, path, headers, ok=(200, 201), **kw):
    r = httpx.request(method, BASE + path, headers=headers, timeout=30, **kw)
    if r.status_code not in ok:
        sys.exit(f"payroll_api: {method} {path} -> HTTP {r.status_code}")
    return r.json() if r.content else None


def pages(path, headers_fn):
    cursor = None
    while True:
        params = {"limit": 100, **({"cursor": cursor} if cursor else {})}
        page = call("GET", path, headers_fn(), params=params)
        yield from page["items"]
        cursor = page.get("next_cursor")
        if not cursor:
            return


def prep():
    return user(PREPARER, ["payroll_admin"])


def approver():
    return user(APPROVER, ["approver"])


cmd, args = sys.argv[1], sys.argv[2:]
if cmd == "new-run":
    used = {r["period_start"] for r in pages("/v1/pay-runs", prep) if r["schedule"] == "monthly"}
    today = datetime.now(UTC).date()
    start = next(
        s
        for i in range(2, 2400)
        if (s := f"{(t := today.year * 12 + today.month - 1 + i) // 12:04d}-{t % 12 + 1:02d}-01")
        not in used
    )
    run = call("POST", "/v1/pay-runs", prep(), json={"schedule": "monthly", "period_start": start})
    rid = run["id"]
    hourly = [
        {"employee_id": p["employee_id"], "hours_hundredths": 16000}
        for p in pages(f"/v1/pay-runs/{rid}/payslips", prep)
        if p["pay_type"] == "hourly"
    ]
    for i in range(0, len(hourly), 500):
        call("PUT", f"/v1/pay-runs/{rid}/hours", prep(), json={"entries": hourly[i : i + 500]})
    call("POST", f"/v1/pay-runs/{rid}/calculate", prep())
    print(rid)
elif cmd == "approve":
    key = {"Idempotency-Key": f"drill-{uuid.uuid4()}"}
    print(call("POST", f"/v1/pay-runs/{args[0]}/approve", {**approver(), **key})["status"])
elif cmd == "pick-attempt":
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        for p in pages(f"/v1/pay-runs/{args[0]}/payouts", prep):
            live = p["attempts"] and p["attempts"][-1]["status"] == "requested"
            if live and not p["no_transfer"]:
                print(p["id"], p["current_attempt"])
                sys.exit(0)
        time.sleep(2)
    sys.exit("payroll_api: no payout with a requested attempt")
elif cmd == "result":
    payout, attempt, outcome = args[0], int(args[1]), args[2]
    body = {"attempt": attempt, "outcome": outcome}
    if outcome == "settled":
        body["bank_reference"] = args[3]
    elif len(args) > 3:
        body["reason"] = args[3]
    key = {"Idempotency-Key": f"drill-{uuid.uuid4()}"}
    call("POST", f"/v1/internal/payouts/{payout}/result", {**worker(), **key}, json=body)
    print("ok")
elif cmd == "resolve":
    body = {"action": args[2], "note": "alert drill reset"}
    path = f"/v1/pay-runs/{args[0]}/payouts/{args[1]}/resolve"
    print(call("POST", path, approver(), json=body)["status"])
elif cmd == "run-status":
    print(call("GET", f"/v1/pay-runs/{args[0]}", prep())["status"])
else:
    sys.exit(f"payroll_api: unknown command {cmd}")
PY
}

run_final() {
  local s
  s="$(payroll_api run-status "$1")" || return 1
  [[ "$s" == paid || "$s" == partially_failed ]]
}

# approved_run -> creates, calculates and approves a run; prints its id
approved_run() {
  local rid status
  rid="$(payroll_api new-run)" || return 1
  status="$(payroll_api approve "$rid")" || return 1
  log "run $rid approved (status $status)"
  printf '%s' "$rid"
}

# ---------------------------------------------------------------- drills
drill_PayoutDLQ() {
  local entry ok=1
  entry="$(redis_cli payroll REDIS_PAYROLL_PASSWORD XADD stream:payout.requested '*' \
    event_id "drill-$(date +%s)" type payout.requested envelope 'alert-drill: not json')" || return 1
  log "poison entry $entry added to stream:payout.requested"
  delivered PayoutDLQ "$DRILL_TIMEOUT_S" &&
    check_query "Queues: dead-lettered in 15 m" 'sum(increase(queue_messages_total{outcome="dlq"}[15m])) > 0' &&
    check_query "Queues: DLQ length" 'max(queue_dlq_messages) > 0' && ok=0
  # reset: drop the drill's DLQ entry (matched by source_id) as the admin user
  redis_cli default REDIS_ADMIN_PASSWORD --json XRANGE stream:payout.requested.dlq - + |
    python3 -c '
import json, sys
src = sys.argv[1]
for entry_id, fields in json.load(sys.stdin) or []:
    kv = dict(zip(fields[::2], fields[1::2]))
    if kv.get("source_id") == src:
        print(entry_id)
' "$entry" | while read -r id; do
    redis_cli default REDIS_ADMIN_PASSWORD XDEL stream:payout.requested.dlq "$id" >/dev/null
    log "removed DLQ entry $id"
  done
  return "$ok"
}

drill_PayoutNeedsOperator() {
  local rid pick payout attempt ok=1
  service_stop payout-worker
  rid="$(approved_run)" || return 1
  pick="$(payroll_api pick-attempt "$rid")" || return 1
  read -r payout attempt <<<"$pick"
  payroll_api result "$payout" "$attempt" poison schema_invalid >/dev/null || return 1
  log "payout $payout attempt $attempt reported poison: needs_operator"
  delivered PayoutNeedsOperator "$DRILL_TIMEOUT_S" &&
    check_query "Queues: needs operator" 'max(payroll_attempts_needs_operator) > 0' && ok=0
  # reset: Send again (redrive), start the worker, wait for the run to finish
  payroll_api resolve "$rid" "$payout" redrive >/dev/null || ok=1
  service_up payout-worker || ok=1
  wait_until "$DRILL_RECOVERY_WAIT_S" "run $rid final" run_final "$rid" || log "WARNING: run $rid not final yet"
  return "$ok"
}

drill_PayoutLateSettlement() {
  local rid pick payout attempt ok=1
  service_stop payout-worker
  rid="$(approved_run)" || return 1
  pick="$(payroll_api pick-attempt "$rid")" || return 1
  read -r payout attempt <<<"$pick"
  payroll_api result "$payout" "$attempt" rejected account_closed >/dev/null || return 1
  payroll_api result "$payout" "$attempt" settled "DRILL-$(date +%s)" >/dev/null || return 1
  log "payout $payout attempt $attempt: rejected, then settled (late settlement)"
  delivered PayoutLateSettlement "$DRILL_TIMEOUT_S" &&
    check_query "Queues: late settlements in 15 m" 'sum(increase(payroll_late_settlements_total[15m])) > 0' && ok=0
  log "the cause in Loki: {service_name=\"payroll\"} | event=\"payout.late_settlement\" | payout_id=\"$payout\""
  service_up payout-worker || ok=1
  wait_until "$DRILL_RECOVERY_WAIT_S" "run $rid final" run_final "$rid" || log "WARNING: run $rid not final yet"
  return "$ok"
}

drill_PayoutSettlementSlow() {
  local rid ok=1
  curl -fsS --max-time 5 -X POST "$TOXI/proxies/bank-stub" -H 'Content-Type: application/json' \
    -d '{"enabled": false}' >/dev/null || return 1
  log "Toxiproxy: bank-stub proxy disabled (worker -> bank fails to connect)"
  rid="$(approved_run)" || { toxi_reset; return 1; }
  delivered PayoutSettlementSlow "$DRILL_TIMEOUT_S" &&
    check_query "Queues: oldest processing run" 'max(payroll_runs_processing_oldest_age_seconds) > 60' &&
    check_query "Bank: failed bank calls" 'sum(increase(external_requests_total{provider="bank", outcome!="ok"}[5m])) > 0' && ok=0
  toxi_reset || ok=1
  # indeterminate attempts wait for re-publish (PAYOUT_REPUBLISH_AFTER_S, 360 s)
  wait_until "$DRILL_RECOVERY_WAIT_S" "run $rid final" run_final "$rid" || log "WARNING: run $rid not final yet"
  return "$ok"
}

drill_PayoutsStuck() {
  local rid ok=1
  service_stop payout-worker
  log "payout-worker stopped"
  rid="$(approved_run)" || { service_up payout-worker; return 1; }
  delivered PayoutsStuck "$DRILL_STUCK_TIMEOUT_S" &&
    check_query "Queues: stuck payouts" 'max(payroll_payouts_stuck) > 0' &&
    check_query "Queues: consumer lag (redis-exporter)" 'max(redis_stream_group_lag{stream="stream:payout.requested"}) > 0' && ok=0
  service_up payout-worker || ok=1
  wait_until "$DRILL_RECOVERY_WAIT_S" "run $rid final" run_final "$rid" || log "WARNING: run $rid not final yet"
  wait_until "$DRILL_RECOVERY_WAIT_S" "no stuck payouts" prom_has 'max(payroll_payouts_stuck) == 0' ||
    log "WARNING: payroll_payouts_stuck still above 0"
  return "$ok"
}

load_login() { # one load loop: failed sign-ins through the BFF while identity is down
  local body='{"email":"alert-drill@payrun.example.com","password":"not-the-password-1"}'
  while :; do
    curl -s -o /dev/null --max-time 10 -X POST "$WEB/api/auth/login" \
      -H 'Content-Type: application/json' -H "Origin: $WEB" -d "$body" || true
  done
}

drill_BffAvailabilityFastBurn() {
  local i ok=1
  service_stop identity
  log "identity stopped; $DRILL_LOAD_WORKERS sign-in loops against $WEB/api/auth/login"
  for ((i = 0; i < DRILL_LOAD_WORKERS; i++)); do
    load_login &
    LOAD_PIDS+=("$!")
  done
  delivered BffAvailabilityFastBurn "$DRILL_TIMEOUT_S" &&
    check_query "Journeys: J1 5xx at the login route" 'sum(rate(bff_request_duration_seconds_count{route="/api/auth/login", status=~"5.."}[5m])) > 0' &&
    check_query "Journeys: burn rate 5 m above 14.4" 'sli:bff_errors:ratio_rate5m / 0.005 > 14.4' && ok=0
  stop_load
  service_up identity || ok=1
  wait_until "$DRILL_RECOVERY_WAIT_S" "5 m error ratio below 14.4x" prom_lacks 'sli:bff_errors:ratio_rate5m > 0.072' ||
    log "WARNING: the 5 m error ratio is still high"
  return "$ok"
}

# ---------------------------------------------------------------- main
run_drill() {
  local alert="$1" started
  group "drill: $alert"
  started=$(date +%s)
  wait_until "$DRILL_QUIET_WAIT_S" "$alert not firing before the drill" alert_quiet "$alert" || return 1
  sink_clear || return 1
  "drill_$alert" || return 1
  log "$alert drilled in $(($(date +%s) - started))s"
}

curl -fsS --max-time 5 "$PROM/-/ready" >/dev/null || die "Prometheus is not ready at $PROM"
curl -fsS --max-time 5 "$ALERTMANAGER/-/ready" >/dev/null || die "Alertmanager is not ready at $ALERTMANAGER"
curl -fsS --max-time 5 "$SINK/healthz" >/dev/null || die "alert-sink is not reachable at $SINK"
curl -fsS --max-time 5 "$TOXI/version" >/dev/null || die "Toxiproxy is not reachable at $TOXI (test profile)"
curl -fsS --max-time 5 "$PAYROLL/readyz" >/dev/null || die "payroll is not ready at $PAYROLL"

selected=("$@")
((${#selected[@]})) || selected=("${PAGING_ALERTS[@]}")
for a in "${selected[@]}"; do
  case " ${PAGING_ALERTS[*]} " in
    *" $a "*) ;;
    *) die "not a paging alert: $a (one of: ${PAGING_ALERTS[*]})" ;;
  esac
done

passed=()
failed=()
DRILLS_STARTED=1
for a in "${selected[@]}"; do
  if run_drill "$a"; then passed+=("$a"); else failed+=("$a"); fi
  safety_reset
done
endgroup

log "drilled: ${passed[*]:-none}"
if ((${#failed[@]})); then
  die "failed: ${failed[*]}"
fi
