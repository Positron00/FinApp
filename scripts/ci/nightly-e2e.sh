#!/usr/bin/env bash
# Nightly job `e2e-repeat` (platform LLD §3.15, §11.2; testing.md: a flake is a bug).
#
# Usage: scripts/ci/nightly-e2e.sh            (E2E_REPEAT=3 by default)
#
# Runs the FULL suite (E2E_SUITE=full: J1–J5, role changes, errors, trace, axe; C51 since F6)
# through scripts/ci/e2e.sh three times in a row, each on a fresh stack (e2e.sh always ends with
# `stack.sh down -v`). An E2E_SUITE other than full in the environment (nightly.yml still sets
# `skeleton` from before F3) is overridden with a warning: the nightly repeat exists to catch
# flaky journeys. Logs of each run go to ci-logs/e2e-run-<n>.log; the first failure stops the
# job with that run's artefacts in place.
# shellcheck source=scripts/ci/lib.sh disable=SC1091  # followed with -x; single-file runs (pre-commit) cannot
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

if [[ -n "${E2E_SUITE:-}" && "$E2E_SUITE" != full ]]; then
  msg="E2E_SUITE=$E2E_SUITE ignored: the nightly repeat runs the full suite (C51)"
  log "$msg"
  [[ -n "${GITHUB_ACTIONS:-}" ]] && echo "::warning title=nightly e2e::$msg"
fi
export E2E_SUITE=full
runs="${E2E_REPEAT:-3}"
for ((n = 1; n <= runs; n++)); do
  group "e2e run $n/$runs ($E2E_SUITE)"
  if ! scripts/ci/e2e.sh 2>&1 | tee "ci-logs/e2e-run-$n.log"; then
    die "e2e run $n/$runs failed (log: ci-logs/e2e-run-$n.log)"
  fi
  # Keep each run's Playwright report; the next run overwrites ci-logs/playwright.
  [[ -d ci-logs/playwright ]] && rm -rf "ci-logs/playwright-run-$n" && cp -R ci-logs/playwright "ci-logs/playwright-run-$n"
done
endgroup
log "$runs consecutive e2e runs passed"
