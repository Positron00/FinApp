#!/usr/bin/env bash
# The local stack (platform LLD §3.15; PAY-NFR-13, PAY-NFR-15).
#
# Usage: scripts/stack.sh <command> [args]
#   up [--profiles core,obs,test] [--check-startup]   build, start and wait until healthy
#   down [-v]                                         stop every profile (-v also drops volumes)
#   logs [svc...]                                     logs with timestamps
#   ps                                                container status
#   reset [--profiles ...]                            down -v, then up with the same profiles
#   seed [args]                                       uv run python scripts/seed.py [args]
#   psql <role>                                       psql in the postgres container as a role
#
# Sources .env (running scripts/dev-env.sh first if it is missing). Default profiles are
# core,obs,test (the e2e set); with the test profile on, compose.test.yaml is added.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ENV_FILE="${ENV_FILE:-.env}"
[[ -f "$ENV_FILE" ]] || scripts/dev-env.sh --env-file "$ENV_FILE"

ALL_PROFILES=(--profile core --profile obs --profile test)
WAIT_TIMEOUT="${STACK_WAIT_TIMEOUT:-300}"
STARTUP_LIMIT_S=20

compose_cmd() {
  # compose_cmd <profiles-csv> -> prints nothing; sets the COMPOSE array
  local profiles="$1"
  COMPOSE=(docker compose --env-file "$ENV_FILE" -f deploy/compose/compose.yaml)
  if [[ ",$profiles," == *",test,"* ]]; then
    COMPOSE+=(-f deploy/compose/compose.test.yaml)
  fi
  local p
  IFS=',' read -r -a _ps <<<"$profiles"
  for p in "${_ps[@]}"; do COMPOSE+=(--profile "$p"); done
}

full_compose() {
  COMPOSE=(docker compose --env-file "$ENV_FILE" -f deploy/compose/compose.yaml
    -f deploy/compose/compose.test.yaml "${ALL_PROFILES[@]}")
}

log() { printf '[stack] %s\n' "$*" >&2; }

# Seconds from container start to its first passing health check, per app service (PAY-NFR-15).
startup_report() {
  local check="$1" out="ci-logs/startup.json" slow=0
  mkdir -p ci-logs
  local json="{" sep=""
  for svc in identity payroll payout-worker bank-stub web; do
    local cid
    cid="$("${COMPOSE[@]}" ps -q "$svc" 2>/dev/null || true)"
    [[ -n "$cid" ]] || continue
    local secs
    # shellcheck disable=SC2016  # a Python program, not a shell expression
    secs="$(docker inspect "$cid" | python3 -c '
import json, sys
from datetime import datetime
def ts(s):
    s = s.rstrip("Z")
    head, _, frac = s.partition(".")
    return datetime.fromisoformat(head + "." + (frac[:6] if frac else "0"))
c = json.load(sys.stdin)[0]
start = ts(c["State"]["StartedAt"])
ok = [ts(h["End"].split("+")[0]) for h in (c["State"].get("Health") or {}).get("Log", []) if h["ExitCode"] == 0]
# Docker keeps only the last 5 probes; the earliest kept success is an upper bound when the
# container just started, which is the case right after `up --wait`.
print(round((min(ok) - start).total_seconds(), 1) if ok else -1)
')"
    json+="${sep}\"${svc}\": ${secs}"
    sep=", "
    if [[ "$secs" == "-1" ]] || python3 -c "import sys; sys.exit(0 if float('$secs') > $STARTUP_LIMIT_S else 1)"; then
      slow=1
      log "startup: $svc took ${secs}s (limit ${STARTUP_LIMIT_S}s)"
    fi
  done
  json+="}"
  printf '%s\n' "$json" >"$out"
  log "startup times: $json (ci-logs/startup.json)"
  if [[ "$check" == 1 && "$slow" == 1 ]]; then
    log "startup check failed (PAY-NFR-15)"
    return 1
  fi
}

on_up_failure() {
  log "stack did not become healthy; status and recent logs follow"
  "${COMPOSE[@]}" ps -a >&2 || true
  local svc
  for svc in $("${COMPOSE[@]}" ps -a --format '{{.Service}} {{.State}} {{.Health}} {{.ExitCode}}' |
    awk '($2 != "running" && $4 != "0") || $3 == "unhealthy" || $3 == "starting" {print $1}'); do
    echo "----- $svc (last 100 lines) -----" >&2
    "${COMPOSE[@]}" logs --no-color --tail 100 "$svc" >&2 || true
  done
}

cmd_up() {
  local profiles="core,obs,test" check=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --profiles) profiles="$2"; shift 2 ;;
      --profiles=*) profiles="${1#*=}"; shift ;;
      --check-startup) check=1; shift ;;
      *) echo "stack.sh up: unknown argument: $1" >&2; exit 2 ;;
    esac
  done
  scripts/dev-env.sh --env-file "$ENV_FILE" >/dev/null  # refresh derived keys (APP_VERSION, ports)
  compose_cmd "$profiles"
  log "up (profiles: $profiles)"
  if ! "${COMPOSE[@]}" up -d --build --wait --wait-timeout "$WAIT_TIMEOUT" --remove-orphans; then
    on_up_failure
    exit 1
  fi
  "${COMPOSE[@]}" ps
  startup_report "$check"
}

cmd_down() {
  full_compose
  "${COMPOSE[@]}" down --remove-orphans "$@"
}

case "${1:-}" in
  up) shift; cmd_up "$@" ;;
  down) shift; cmd_down "$@" ;;
  logs) shift; full_compose; "${COMPOSE[@]}" logs --no-color --timestamps "$@" ;;
  ps) shift; full_compose; "${COMPOSE[@]}" ps -a "$@" ;;
  reset)
    shift
    cmd_down -v
    cmd_up "$@"
    ;;
  seed) shift; exec uv run python scripts/seed.py "$@" ;;
  psql)
    shift
    role="${1:?usage: stack.sh psql <role> (postgres, identity_app, payroll_app, ...)}"
    full_compose
    case "$role" in
      postgres) pw_key=POSTGRES_PASSWORD ;;
      identity_app) pw_key=IDENTITY_DB_PASSWORD ;;
      identity_migrator) pw_key=IDENTITY_DB_MIGRATOR_PASSWORD ;;
      payroll_app) pw_key=PAYROLL_DB_PASSWORD ;;
      payroll_migrator) pw_key=PAYROLL_DB_MIGRATOR_PASSWORD ;;
      payouts_app) pw_key=PAYOUTS_DB_PASSWORD ;;
      payouts_migrator) pw_key=PAYOUTS_DB_MIGRATOR_PASSWORD ;;
      bank_stub_app) pw_key=BANK_STUB_DB_PASSWORD ;;
      bank_stub_migrator) pw_key=BANK_STUB_DB_MIGRATOR_PASSWORD ;;
      *) echo "stack.sh psql: unknown role: $role" >&2; exit 2 ;;
    esac
    pw="$(grep -E "^${pw_key}=" "$ENV_FILE" | tail -n1 | cut -d= -f2-)"
    exec "${COMPOSE[@]}" exec -e PGPASSWORD="$pw" postgres psql -h 127.0.0.1 -U "$role" -d payrun
    ;;
  -h|--help|"") sed -n '2,14p' "$0" ;;
  *) echo "stack.sh: unknown command: $1" >&2; exit 2 ;;
esac
