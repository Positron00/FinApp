#!/usr/bin/env bash
# Nightly job `load` (platform LLD §3.15, §11.2; web LLD §9.3; PAY-NFR-04, PAY-NFR-05, PAY-NFR-06).
#
# Usage: scripts/ci/nightly-load.sh
#        LOAD_SCENARIOS="settlement" FULL_HOLD=2m SOAK_DURATION=10m ...   (a shorter local check)
#
# Stack up, `seed.py --employees 500`, then the k6 scenarios, each through scripts/ci/k6.sh:
#   load/full.js        ramping-arrival-rate to 30 journey operations/s, held 10 min; errors < 1%,
#                       p95 < 300 ms (simple) and < 1.5 s (calculate) (PAY-NFR-05)
#   load/soak.js        5 operations/s for 30 min; the same thresholds, plus no upward drift: the
#                       p95 of the last 5 min within 20% of the first 5 min (checked here, from the
#                       summary export's window:first / window:last sub-metrics)
#   load/settlement.js  five 500-employee runs approved as approver@payrun.example.com (R22);
#                       settlement p95 <= 60 s (PAY-NFR-06), cross-checked here against payroll's
#                       payroll_payout_settlement_seconds histogram in Prometheus
# Every scenario runs even when an earlier one fails; the job fails if any did. Summaries go to
# load/results/<scenario>.json, which the workflow uploads.
# shellcheck source=scripts/ci/lib.sh disable=SC1091  # followed with -x; single-file runs (pre-commit) cannot
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need docker uv python3 curl

export SEED_JSON="$CI_ROOT/ci-logs/seed.json"
on_exit 'scripts/stack.sh logs >ci-logs/compose.log 2>&1 || true; scripts/stack.sh down -v >/dev/null 2>&1 || true'
read -r -a scenarios <<<"${LOAD_SCENARIOS:-full soak settlement}"

group "stack up"
mkdir -p ci-logs load/results
scripts/dev-env.sh >/dev/null
bake_images "$(env_val APP_VERSION)"
scripts/stack.sh up
uv sync --locked --all-packages

group "seed (500 employees)"
uv run python scripts/seed.py --employees 500 --out ci-logs/seed.json

# The soak's drift check: p95 of the last window within 20% of the first (simple requests).
soak_drift() {
  python3 - load/results/soak.json <<'PY'
import json
import sys

metrics = json.load(open(sys.argv[1], encoding="utf-8"))["metrics"]
first = metrics.get("http_req_duration{window:first,kind:simple}", {}).get("p(95)")
last = metrics.get("http_req_duration{window:last,kind:simple}", {}).get("p(95)")
if first is None or last is None:
    sys.exit("soak drift: the window:first / window:last sub-metrics are missing")
verdict = "ok" if last <= first * 1.2 else "FAILED"
print(f"soak drift: p95 first window {first:.1f} ms, last window {last:.1f} ms (limit +20%): {verdict}")
sys.exit(0 if verdict == "ok" else 1)
PY
}

# Cross-check: payroll's own settlement histogram over the settlement step (PAY-NFR-06).
settlement_prometheus() {
  local since="$1" window p95
  window="$(($(date +%s) - since + 30))"
  p95="$(curl -fsS -G "http://127.0.0.1:$(env_val PROMETHEUS_PORT)/api/v1/query" \
    --data-urlencode "query=histogram_quantile(0.95, sum by (le) (increase(payroll_payout_settlement_seconds_bucket[${window}s])))" |
    python3 -c 'import json, sys; r = json.load(sys.stdin)["data"]["result"]; print(r[0]["value"][1] if r else "none")')"
  log "Prometheus payroll_payout_settlement_seconds p95 over the last ${window}s: ${p95} s"
  [[ "$p95" != none && "$p95" != NaN ]] || { log "no settlement observations in Prometheus"; return 1; }
  python3 -c "import sys; sys.exit(0 if float('$p95') <= 60 else 1)"
}

failed=() ran=()
for scenario in "${scenarios[@]}"; do
  group "k6 $scenario"
  [[ -f "load/$scenario.js" ]] || die "load/$scenario.js does not exist"
  started="$(date +%s)"
  extra=()
  [[ "$scenario" == full && -n "${FULL_HOLD:-}" ]] && extra+=(-e "FULL_HOLD=$FULL_HOLD")
  [[ "$scenario" == soak && -n "${SOAK_DURATION:-}" ]] && extra+=(-e "SOAK_DURATION=$SOAK_DURATION")
  if SEED_JSON=ci-logs/seed.json scripts/ci/k6.sh "load/$scenario.js" ${extra[@]+"${extra[@]}"}; then
    ran+=("$scenario")
  else
    failed+=("$scenario")
  fi
  if [[ "$scenario" == soak ]] && ! soak_drift; then failed+=("soak-drift"); fi
  if [[ "$scenario" == settlement ]] && ! settlement_prometheus "$started"; then
    failed+=("settlement-prometheus")
  fi
done
endgroup

log "scenarios passed: ${ran[*]:-none}; failed: ${failed[*]:-none}"
if ((${#failed[@]} > 0)); then
  [[ -n "${GITHUB_ACTIONS:-}" ]] && echo "::error title=nightly load::failed: ${failed[*]}"
  die "load scenarios failed: ${failed[*]}"
fi
