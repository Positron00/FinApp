#!/usr/bin/env bash
# CI job `python` (platform LLD §3.15, §12; PAY-NFR-12; C58, C63, C64).
#
# Usage: scripts/ci/python.sh <member>
#   member: pycommon | identity | payroll | payout_worker | bank_stub | platform
#
# Lint, format, types, then tests with the coverage gate at its final threshold:
#   unit         --disable-socket --allow-unix-socket, --cov-fail-under=0           (C58, C64)
#   contract     --cov-append --cov-fail-under=0                                     (services only)
#   integration  --cov-append --cov-fail-under=<90 pycommon | 85 services>           (C64)
# The threshold is also each member's `fail_under` (C63), so the combined unit + integration report
# is what is gated. Suites run in separate pytest processes (C61: OTel metric globals leak between
# suites in one process). Member `platform` lints scripts/ and tests/, and runs tests/platform
# without a coverage gate (test_seed.py needs the running stack, so e2e.sh runs it instead).
# Needs Docker for Testcontainers. Writes coverage.xml at the repository root.
# shellcheck source=scripts/ci/lib.sh disable=SC1091  # followed with -x; single-file runs (pre-commit) cannot
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# An explicit check, not ${1:?}: on bash 3.2 (macOS) a failed ${1:?} reaches the EXIT trap with
# status 0, so the script would log "passed".
[[ $# -ge 1 && -n "$1" ]] || die "usage: scripts/ci/python.sh <pycommon|identity|payroll|payout_worker|bank_stub|platform>"
member="$1"
need uv docker

case "$member" in
  pycommon) dir=libs/pycommon pkg=pycommon threshold=90 ;;
  identity | payroll | payout_worker | bank_stub) dir="services/$member" pkg="$member" threshold=85 ;;
  platform) dir=. pkg="" threshold="" ;;
  *) die "unknown member: $member" ;;
esac

group "uv sync"
uv sync --locked --all-packages
endgroup

rm -f coverage.xml

if [[ "$member" == platform ]]; then
  paths=(scripts tests deploy/observability/alert-sink)
  group "ruff check"
  uv run ruff check "${paths[@]}"
  group "ruff format --check"
  uv run ruff format --check "${paths[@]}"
  group "mypy --strict"
  # Two runs: tests/platform and tests/resilience both have a top-level conftest module.
  uv run mypy --strict scripts tests/platform deploy/observability/alert-sink
  uv run mypy --strict tests/resilience
  group "pytest tests/platform"
  uv run pytest tests/platform --ignore=tests/platform/test_seed.py -p no:cacheprovider
  endgroup
  exit 0
fi

cd "$dir" || die "no member directory: $dir"
group "ruff check ($member)"
uv run ruff check src tests
group "ruff format --check ($member)"
uv run ruff format --check src tests
group "mypy --strict ($member)"
uv run mypy --strict --config-file "$CI_ROOT/pyproject.toml" src tests

cov=(--cov="$pkg" --cov-branch)
rm -f .coverage .coverage.*

group "pytest unit ($member)"
uv run pytest tests/unit --disable-socket --allow-unix-socket "${cov[@]}" --cov-fail-under=0 \
  --cov-report=

if [[ -d tests/contract ]]; then
  group "pytest contract ($member)"
  uv run pytest tests/contract "${cov[@]}" --cov-append --cov-fail-under=0 --cov-report=
fi

group "pytest integration ($member; coverage gate ${threshold}%)"
uv run pytest tests/integration "${cov[@]}" --cov-append --cov-fail-under="$threshold" \
  --cov-report=term --cov-report="xml:$CI_ROOT/coverage.xml"
endgroup
