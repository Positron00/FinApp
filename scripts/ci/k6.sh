#!/usr/bin/env bash
# Run a k6 script against the running stack (web LLD §9.3; platform LLD §3.15).
#
# Usage: scripts/ci/k6.sh <script.js> [k6 args]   e.g. scripts/ci/k6.sh load/smoke.js
#
# Uses a local k6 when installed (requests go to 127.0.0.1:WEB_PORT). Otherwise it runs the pinned
# grafana/k6 image inside the Compose network and sends requests to http://web:3000, which works
# the same on Docker Desktop and Linux. BASE_URL stays the public origin either way: the BFF checks
# it on mutations (C12). The summary is written to load/results/<script>.json.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
K6_IMAGE="${K6_IMAGE:-grafana/k6:1.8.2}"
script="${1:?usage: scripts/ci/k6.sh <script.js> [k6 args]}"
shift

val() { grep -E "^$1=" .env | tail -n1 | cut -d= -f2-; }
web_port="$(val WEB_PORT)"
project="$(val COMPOSE_PROJECT_NAME)"
seed_json="${SEED_JSON:-ci-logs/seed.json}"
name="$(basename "$script" .js)"
mkdir -p load/results

# Credentials travel in the environment (k6 copies it into __ENV), never on a command line.
LOAD_USER_EMAIL="$(val LOAD_USER_EMAIL)"
LOAD_USER_PASSWORD="$(val LOAD_USER_PASSWORD)"
export LOAD_USER_EMAIL LOAD_USER_PASSWORD
common=(
  -e "BASE_URL=http://127.0.0.1:${web_port}"
  --summary-export "load/results/${name}.json"
)

# k6 open() resolves relative paths against the script, so pass SEED_JSON as an absolute path.
[[ "$seed_json" == /* ]] || seed_json="@ROOT@/$seed_json"
if command -v k6 >/dev/null 2>&1; then
  exec k6 run "${common[@]}" -e "SEED_JSON=${seed_json//@ROOT@/$ROOT}" \
    -e "TARGET_URL=http://127.0.0.1:${web_port}" "$@" "$script"
fi
exec docker run --rm -i --network "${project:-payrun}_payrun" -e LOAD_USER_EMAIL -e LOAD_USER_PASSWORD \
  -u "$(id -u):$(id -g)" -v "$ROOT:/work" -w /work "$K6_IMAGE" \
  run "${common[@]}" -e "SEED_JSON=${seed_json//@ROOT@//work}" \
  -e "TARGET_URL=http://web:3000" "$@" "$script"
