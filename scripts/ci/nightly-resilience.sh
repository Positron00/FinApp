#!/usr/bin/env bash
# Nightly job `resilience` (platform LLD §3.15, §11.2; PAY-NFR-02, PAY-NFR-03; C53).
#
# Usage: scripts/ci/nightly-resilience.sh
#
# Stack up (core, obs, test), the skeleton seed, then every fault test in tests/resilience (HLD §4,
# plan §12.1; task H1), not only the PR smoke subset: worker crash before and after the bank call,
# bank down then up, bank timeout after settling, bank-stub restart, payroll down, Redis down,
# Redis FLUSHALL, Redis restart (PAY-NFR-15), Postgres down, slow DB, identity down, SMTP down.
# Payout timing is production's (C53), so the bank-down, payroll-down and FLUSHALL tests each wait
# out a re-publish (about 8 minutes); the whole suite takes about 35 minutes. Fails if any test
# is skipped: a skip here means the stack wasn't reachable, which would hide the gate. --durations
# prints the slowest tests, for the job's timeout (C70).
# shellcheck source=scripts/ci/lib.sh disable=SC1091  # followed with -x; single-file runs (pre-commit) cannot
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need docker uv python3

on_exit 'scripts/stack.sh logs >ci-logs/compose.log 2>&1 || true; scripts/stack.sh down -v >/dev/null 2>&1 || true'

group "stack up"
scripts/dev-env.sh >/dev/null
bake_images "$(env_val APP_VERSION)"
scripts/stack.sh up
uv sync --locked --all-packages

group "seed"
uv run python scripts/seed.py --skeleton --out ci-logs/seed.json

group "tests/resilience (all faults)"
uv run pytest -p no:cacheprovider tests/resilience -rs --durations=0 --junitxml=ci-logs/resilience.xml
python3 - ci-logs/resilience.xml <<'PY'
import sys
import xml.etree.ElementTree as ET

root = ET.parse(sys.argv[1]).getroot()
suites = [root] if root.tag == "testsuite" else list(root)
tests = sum(int(s.get("tests", 0)) for s in suites)
skipped = sum(int(s.get("skipped", 0)) for s in suites)
if tests == 0 or skipped:
    sys.exit(f"resilience: {tests} test(s), {skipped} skipped; every fault test must run")
print(f"resilience: {tests} test(s) ran")
PY
endgroup
