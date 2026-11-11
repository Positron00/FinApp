#!/usr/bin/env bash
# Nightly job `e2e-repeat` (platform LLD §3.15, §11.2; testing.md: a flake is a bug).
#
# Usage: E2E_SUITE=skeleton|full scripts/ci/nightly-e2e.sh    (nightly.yml: skeleton until F3, C51)
#
# Runs scripts/ci/e2e.sh three times in a row, each on a fresh stack (e2e.sh always ends with
# `stack.sh down -v`). Logs of each run go to ci-logs/e2e-run-<n>.log; the first failure stops the
# job with that run's artefacts in place.
# shellcheck source=scripts/ci/lib.sh disable=SC1091  # followed with -x; single-file runs (pre-commit) cannot
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

export E2E_SUITE="${E2E_SUITE:-full}"
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
