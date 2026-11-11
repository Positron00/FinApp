#!/usr/bin/env bash
# Nightly job `resilience` (platform LLD §3.15, §11.2; PAY-NFR-02, PAY-NFR-03; C53).
#
# Usage: scripts/ci/nightly-resilience.sh
#
# Stack up (core, obs, test), the skeleton seed, then every fault test in tests/resilience (plan
# §12.1), not only the PR smoke subset. Fails if any test is skipped: a skip here means the stack
# wasn't reachable, which would hide the gate. Today the suite holds the slow-DB test; the rest
# of the faults arrive with task H1 and run here unchanged.
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
uv run pytest -p no:cacheprovider tests/resilience -rs --junitxml=ci-logs/resilience.xml
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
