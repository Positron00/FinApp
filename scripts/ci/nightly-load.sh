#!/usr/bin/env bash
# Nightly job `load` (platform LLD §3.15, §11.2; PAY-NFR-05, PAY-NFR-06; C38).
#
# Usage: scripts/ci/nightly-load.sh
#
# Stack up, `seed.py --employees 500`, then the k6 scenarios: load/full.js (30 req/s, 10 min),
# load/soak.js (5 req/s, 30 min; no memory or lag growth) and load/settlement.js (settlement p95
# <= 60 s for 500 employees, approving as approver@payrun.example.com, R22). Summaries go to
# load/results/<scenario>.json, which the workflow uploads.
#
# STUB STATUS (2026-11-10): full.js and settlement.js arrive with task F6 and soak.js with H2. Until
# a scenario file exists this script says so (a GitHub warning annotation, and NOT RUN in the log)
# and runs load/smoke.js in its place as a sanity check. It never reports a missing scenario as
# passed: the summary at the end lists which scenarios ran.
# shellcheck source=scripts/ci/lib.sh disable=SC1091  # followed with -x; single-file runs (pre-commit) cannot
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need docker uv

export SEED_JSON="$CI_ROOT/ci-logs/seed.json"
on_exit 'scripts/stack.sh logs >ci-logs/compose.log 2>&1 || true; scripts/stack.sh down -v >/dev/null 2>&1 || true'

group "stack up"
scripts/dev-env.sh >/dev/null
bake_images "$(env_val APP_VERSION)"
scripts/stack.sh up
uv sync --locked --all-packages

group "seed (500 employees)"
uv run python scripts/seed.py --employees 500 --out ci-logs/seed.json

ran=() not_built=()
for scenario in full soak settlement; do
  group "k6 $scenario"
  if [[ -f "load/$scenario.js" ]]; then
    SEED_JSON=ci-logs/seed.json scripts/ci/k6.sh "load/$scenario.js"
    ran+=("$scenario")
  else
    not_built+=("$scenario")
    msg="load/$scenario.js is NOT BUILT YET (tasks F6/H2): scenario NOT RUN"
    log "$msg"
    [[ -n "${GITHUB_ACTIONS:-}" ]] && echo "::warning title=nightly load::$msg"
  fi
done

if ((${#not_built[@]} > 0)); then
  group "k6 smoke (stand-in while scenarios are missing)"
  SEED_JSON=ci-logs/seed.json scripts/ci/k6.sh load/smoke.js
fi
endgroup
log "scenarios run: ${ran[*]:-none}; not built yet: ${not_built[*]:-none}"
