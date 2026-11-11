#!/usr/bin/env bash
# Schemathesis against each running service (platform LLD §3.15; testing.md §4; R30, C35).
#
# Usage: scripts/ci/schemathesis.sh [identity|payroll|bank_stub ...]   (default: all three)
#        MAX_EXAMPLES=500 scripts/ci/schemathesis.sh                    (nightly)
#
# Every request gets a fresh internal JWT from scripts/ci/schemathesis_auth.py (bank-stub: its API
# key). Reports go to ci-logs/schemathesis/<service>.xml. Exits 1 if any service fails.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

val() { grep -E "^$1=" .env | tail -n1 | cut -d= -f2-; }
services=("$@")
[[ ${#services[@]} -gt 0 ]] || services=(identity payroll bank_stub)
export SEED_JSON="${SEED_JSON:-ci-logs/seed.json}"
export SCHEMATHESIS_HOOKS=scripts.ci.schemathesis_auth
mkdir -p ci-logs/schemathesis

status=0
for svc in "${services[@]}"; do
  case "$svc" in
    identity) port="$(val IDENTITY_PORT)" ;;
    payroll) port="$(val PAYROLL_PORT)" ;;
    bank_stub | bank-stub) svc=bank_stub; port="$(val BANK_STUB_PORT)" ;;
    *) echo "schemathesis.sh: unknown service: $svc" >&2; exit 2 ;;
  esac
  echo "::group::schemathesis $svc (127.0.0.1:$port)"
  if ! PAYRUN_SERVICE="$svc" uv run schemathesis --config-file scripts/ci/schemathesis.toml run \
    "services/$svc/openapi.json" --url "http://127.0.0.1:$port" \
    --max-examples "${MAX_EXAMPLES:-50}" --seed "${SCHEMATHESIS_SEED:-609}" \
    --report junit --report-junit-path "ci-logs/schemathesis/$svc.xml"; then
    status=1
  fi
  echo "::endgroup::"
done
exit "$status"
