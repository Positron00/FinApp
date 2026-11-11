#!/usr/bin/env bash
# CI job `e2e` (platform LLD §3.15, §12; PAY-NFR-12, PAY-NFR-13, PAY-NFR-15; C38, C51, C65).
#
# Usage: scripts/ci/e2e.sh                       the full suite (default; ci.yml since F6, C51)
#        E2E_SUITE=skeleton scripts/ci/e2e.sh    only the walking skeleton (@skeleton)
#        E2E_KEEP_STACK=1 ...                    leave the stack running afterwards (debugging)
#
#   1. scripts/dev-env.sh (throwaway secrets; an existing .env is kept), images via bake (the CI
#      gha cache scopes of the images job), scripts/stack.sh up --check-startup (core, obs, test;
#      every service ready <= 20 s after its migrations, PAY-NFR-15).
#   2. Seed: skeleton `seed.py --skeleton`; full `seed.py` then `seed.py --employees 500` (C33, C54).
#   3. Playwright: skeleton `--grep @skeleton`; full `--grep "@journeys|@trace|@a11y"` (J1–J5,
#      role changes, errors, the trace test and axe; web LLD §9.2).
#   4. Both suites: tests/platform/test_seed.py -m stack (PAY-609), k6 smoke, Schemathesis per
#      service (after Playwright and k6: its stateful phase deletes and changes seeded data; C65),
#      the PR fault smoke tests (tests/resilience -m smoke, C38: slow DB and identity down; the
#      other faults run nightly in nightly-resilience.sh), check_metrics.sh. The seed tests
#      and the fault smoke must run every test: a skip (stack or summary missing) fails the job.
# On failure the Compose logs go to ci-logs/compose.log; the stack is always torn down with -v.
# shellcheck source=scripts/ci/lib.sh disable=SC1091  # followed with -x; single-file runs (pre-commit) cannot
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need docker uv pnpm curl python3

suite="${E2E_SUITE:-full}"
case "$suite" in
  skeleton | full) ;;
  *) die "E2E_SUITE must be skeleton or full, not '$suite'" ;;
esac
log "suite: $suite"
export SEED_JSON="$CI_ROOT/ci-logs/seed.json"

on_failure_logs() {
  if ((CI_EXIT_CODE != 0)); then
    log "collecting Compose logs into ci-logs/compose.log"
    scripts/stack.sh ps >ci-logs/compose-ps.txt 2>&1 || true
    scripts/stack.sh logs >ci-logs/compose.log 2>&1 || true
  fi
  if [[ "${E2E_KEEP_STACK:-0}" != 1 ]]; then
    log "stack down -v"
    scripts/stack.sh down -v >/dev/null 2>&1 || log "stack down failed"
  fi
}
on_exit on_failure_logs

# all_ran LABEL JUNIT_XML: fail unless at least one test ran and none skipped. The stack suites
# (test_seed.py, tests/resilience -m smoke) skip themselves when the stack or the seed summary is
# missing; in this job a skip would turn a broken gate green.
all_ran() {
  python3 - "$1" "$2" <<'PY'
import sys
import xml.etree.ElementTree as ET

label, path = sys.argv[1], sys.argv[2]
root = ET.parse(path).getroot()
suites = [root] if root.tag == "testsuite" else list(root)
tests = sum(int(s.get("tests", 0)) for s in suites)
skipped = sum(int(s.get("skipped", 0)) for s in suites)
if tests == 0 or skipped:
    sys.exit(f"{label}: {tests} test(s), {skipped} skipped; every test must run")
print(f"{label}: {tests} test(s) ran")
PY
}

group "dependencies"
uv sync --locked --all-packages
pnpm install --frozen-lockfile
if [[ "${PLAYWRIGHT_SKIP_INSTALL:-0}" != 1 ]]; then
  pnpm --filter web exec playwright install chromium # CI installs it with --with-deps first
fi

group "dev-env and images"
scripts/dev-env.sh >/dev/null
rm -f ci-logs/seed.json ci-logs/startup.json
bake_images "$(env_val APP_VERSION)"

group "stack up (core, obs, test; start-up check)"
scripts/stack.sh up --check-startup

group "seed ($suite)"
if [[ "$suite" == skeleton ]]; then
  uv run python scripts/seed.py --skeleton --out ci-logs/seed.json
else
  uv run python scripts/seed.py --out ci-logs/seed.json
  uv run python scripts/seed.py --employees 500 --out ci-logs/seed.json
fi

group "playwright ($suite)"
if [[ "$suite" == skeleton ]]; then
  pnpm --filter web exec playwright test --grep @skeleton
else
  pnpm --filter web exec playwright test --grep "@journeys|@trace|@a11y"
fi

group "seed tests (PAY-609)"
uv run pytest -p no:cacheprovider tests/platform/test_seed.py -m stack --junitxml=ci-logs/seed-tests.xml
all_ran "seed tests" ci-logs/seed-tests.xml

group "k6 smoke"
# Before Schemathesis: its stateful phase deletes the still-calculated runs.current of the full
# seed and changes seeded users and settings (C65 only needs it after Playwright).
# k6.sh maps a repo-relative SEED_JSON into its container; the absolute one is for Playwright.
SEED_JSON=ci-logs/seed.json scripts/ci/k6.sh load/smoke.js

group "schemathesis (identity, payroll, bank_stub)"
scripts/ci/schemathesis.sh

group "fault smoke (tests/resilience -m smoke; C38)"
uv run pytest -p no:cacheprovider tests/resilience -m smoke --junitxml=ci-logs/resilience-smoke.xml
all_ran "fault smoke" ci-logs/resilience-smoke.xml

group "metric names"
scripts/ci/check_metrics.sh
endgroup
