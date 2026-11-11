#!/usr/bin/env bash
# Nightly job `schemathesis-deep` (platform LLD §3.15, §11.2; R30, C35, C65).
#
# Usage: scripts/ci/nightly-schemathesis.sh
#
# Stack up, the skeleton seed, then Schemathesis against identity, payroll and bank-stub with
# 500 examples per operation (the PR run uses 50), same checks and auth hooks as e2e.sh.
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

group "seed"
uv run python scripts/seed.py --skeleton --out ci-logs/seed.json

group "schemathesis (500 examples per operation)"
MAX_EXAMPLES="${MAX_EXAMPLES:-500}" scripts/ci/schemathesis.sh
endgroup
