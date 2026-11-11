#!/usr/bin/env bash
# Generate or complete the git-ignored .env for dev and CI (PAY-NFR-12; platform LLD §3.15).
#
# Usage: scripts/dev-env.sh [--env-file PATH] [--k8s-dev]
#
# - Idempotent: existing values are kept; only missing keys are appended. Derived keys (ports,
#   public-key sets, APP_VERSION) are recomputed on every run.
# - Never prints a secret value: only the names of keys it added.
# - CI runs this too, so no repository secrets are needed. uat/prod never use this script.
set -euo pipefail

ENV_FILE=".env"
K8S_DEV=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --k8s-dev) K8S_DEV=1; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "dev-env.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
done

# --- OpenSSL 3 is needed for Ed25519 (macOS /usr/bin/openssl is LibreSSL) -------------------
find_openssl() {
  local candidates=()
  if command -v brew >/dev/null 2>&1; then
    candidates+=("$(brew --prefix openssl@3 2>/dev/null)/bin/openssl")
  fi
  candidates+=("$(command -v openssl || true)")
  for c in "${candidates[@]}"; do
    if [[ -x "$c" ]] && "$c" version 2>/dev/null | grep -q '^OpenSSL 3'; then
      echo "$c"; return 0
    fi
  done
  return 1
}
OPENSSL="$(find_openssl)" || {
  echo "dev-env.sh: OpenSSL 3 is required (macOS: brew install openssl@3)" >&2
  exit 1
}

umask 077
touch "$ENV_FILE"
chmod 600 "$ENV_FILE"

ADDED=()

has_key() { grep -qE "^$1=" "$ENV_FILE"; }

# Read a value back from .env, undoing our own quoting (PEMs: "...\n..."; JSON: '...').
get_val() {
  local line
  line="$(grep -E "^$1=" "$ENV_FILE" | tail -n1)"
  line="${line#*=}"
  if [[ "$line" == \"*\" ]]; then
    line="${line:1:${#line}-2}"
    printf '%b' "${line//\\n/\\n}"
  elif [[ "$line" == \'*\' ]]; then
    printf '%s' "${line:1:${#line}-2}"
  else
    printf '%s' "$line"
  fi
}

# Append KEY=VALUE when KEY is missing. Mode: plain | pem | json.
put_if_missing() {
  local key="$1" value="$2" mode="${3:-plain}"
  has_key "$key" && return 0
  write_kv "$key" "$value" "$mode"
  ADDED+=("$key")
}

# Replace (or add) a derived key.
put_derived() {
  local key="$1" value="$2" mode="${3:-plain}"
  if has_key "$key"; then
    local tmp
    tmp="$(mktemp)"
    grep -vE "^$key=" "$ENV_FILE" > "$tmp" || true
    cat "$tmp" > "$ENV_FILE"
    rm -f "$tmp"
  fi
  write_kv "$key" "$value" "$mode"
}

write_kv() {
  local key="$1" value="$2" mode="$3"
  case "$mode" in
    pem) printf '%s="%s"\n' "$key" "$(printf '%s' "$value" | awk '{printf "%s\\n", $0}')" >> "$ENV_FILE" ;;
    json) printf "%s='%s'\n" "$key" "$value" >> "$ENV_FILE" ;;
    *) printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE" ;;
  esac
}

hex24() { "$OPENSSL" rand -hex 24; }
b64() { "$OPENSSL" rand -base64 "$1" | tr -d '\n'; }
urlsafe20() { "$OPENSSL" rand -base64 30 | tr -d '\n' | tr '+/' '-_' | tr -d '=' | cut -c1-20; }
ed25519() { "$OPENSSL" genpkey -algorithm ed25519 2>/dev/null; }
pubkey_of() { printf '%s\n' "$1" | "$OPENSSL" pkey -pubout 2>/dev/null; }
# JSON-escape a PEM (newlines -> \n).
json_pem() { printf '%s' "$1" | awk 'BEGIN{ORS=""} NR>1{print "\\n"} {print}'; }
today() { date -u +%Y%m%d; }

# --- kept keys --------------------------------------------------------------------------------
put_if_missing ENV dev
put_if_missing COMPOSE_PROJECT_NAME payrun
put_if_missing APP_PORT_BASE 4100
put_if_missing COMPANY_ID 0193a000-0000-7000-8000-000000000001

put_if_missing POSTGRES_PASSWORD "$(hex24)"
for svc in IDENTITY PAYROLL PAYOUTS BANK_STUB; do
  put_if_missing "${svc}_DB_MIGRATOR_PASSWORD" "$(hex24)"
  put_if_missing "${svc}_DB_PASSWORD" "$(hex24)"
done
put_if_missing POSTGRES_EXPORTER_PASSWORD "$(hex24)"

for user in ADMIN WEB IDENTITY PAYROLL PAYOUT_WORKER EXPORTER; do
  put_if_missing "REDIS_${user}_PASSWORD" "$(hex24)"
done

put_if_missing INTERNAL_JWT_PRIVATE_KEY "$(ed25519)" pem
put_if_missing INTERNAL_JWT_KID "dev-web-$(today)"
put_if_missing PAYOUT_WORKER_JWT_PRIVATE_KEY "$(ed25519)" pem
put_if_missing PAYOUT_WORKER_JWT_KID "dev-worker-$(today)"

put_if_missing PAYROLL_FIELD_KEYS "{\"dev-1\": \"$(b64 32)\"}" json
put_if_missing PAYROLL_FIELD_ACTIVE_KID dev-1
put_if_missing AUDIT_IP_HASH_KEY "$(b64 32)"
put_if_missing SESSION_SECRET "$(b64 48)"
put_if_missing BANK_STUB_API_KEY "$("$OPENSSL" rand -hex 32)"
put_if_missing GRAFANA_ADMIN_PASSWORD "$(hex24)"

put_if_missing SMTP_HOST mailpit
put_if_missing SMTP_PORT 1025
put_if_missing ALERT_PAGE_WEBHOOK_URL http://alert-sink:8080/page
put_if_missing ALERT_TICKET_WEBHOOK_URL http://alert-sink:8080/ticket
put_if_missing OTEL_TRACES_SAMPLER parentbased_always_on

put_if_missing SEED_OWNER_EMAIL owner@payrun.example.com
put_if_missing SEED_OWNER_PASSWORD "$(urlsafe20)"
put_if_missing SEED_APPROVER_PASSWORD "$(urlsafe20)"
put_if_missing LOAD_USER_EMAIL load@payrun.example.com
put_if_missing LOAD_USER_PASSWORD "$(urlsafe20)"

# --- derived keys -------------------------------------------------------------------------------
base="$(get_val APP_PORT_BASE)"
put_derived APP_VERSION "$(git rev-parse --short HEAD 2>/dev/null || echo dev)"
put_derived WEB_PORT "$((base + 0))"
put_derived GRAFANA_PORT "$((base + 1))"
put_derived MAILPIT_PORT "$((base + 2))"
put_derived IDENTITY_PORT "$((base + 10))"
put_derived PAYROLL_PORT "$((base + 11))"
put_derived PAYOUT_WORKER_PORT "$((base + 12))"
put_derived BANK_STUB_PORT "$((base + 13))"
put_derived TEMPO_PORT "$((base + 30))"
put_derived LOKI_PORT "$((base + 31))"
put_derived PROMETHEUS_PORT "$((base + 32))"
put_derived ALERTMANAGER_PORT "$((base + 33))"
put_derived TOXIPROXY_PORT "$((base + 34))"
put_derived ALERT_SINK_PORT "$((base + 35))"

web_pub="$(pubkey_of "$(get_val INTERNAL_JWT_PRIVATE_KEY)")"
worker_pub="$(pubkey_of "$(get_val PAYOUT_WORKER_JWT_PRIVATE_KEY)")"
web_kid="$(get_val INTERNAL_JWT_KID)"
worker_kid="$(get_val PAYOUT_WORKER_JWT_KID)"
web_entry="\"${web_kid}\": {\"iss\": \"web\", \"pem\": \"$(json_pem "$web_pub")\"}"
worker_entry="\"${worker_kid}\": {\"iss\": \"payout-worker\", \"pem\": \"$(json_pem "$worker_pub")\"}"
put_derived IDENTITY_INTERNAL_JWT_PUBLIC_KEYS "{${web_entry}}" json
put_derived PAYROLL_INTERNAL_JWT_PUBLIC_KEYS "{${web_entry}, ${worker_entry}}" json

if [[ "$K8S_DEV" == 1 ]]; then
  mkdir -p deploy/k8s/overlays/dev
  grep -vE '^(APP_PORT_BASE|.*_PORT|COMPOSE_PROJECT_NAME)=' "$ENV_FILE" > deploy/k8s/overlays/dev/secrets.env
  chmod 600 deploy/k8s/overlays/dev/secrets.env
fi

if [[ ${#ADDED[@]} -gt 0 ]]; then
  echo "dev-env.sh: added ${#ADDED[@]} keys to $ENV_FILE: ${ADDED[*]}"
else
  echo "dev-env.sh: $ENV_FILE complete (derived keys refreshed)"
fi
