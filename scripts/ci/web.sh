#!/usr/bin/env bash
# CI job `web` (platform LLD §3.15, §12; PAY-NFR-12).
#
# Usage: scripts/ci/web.sh
#
# pnpm install --frozen-lockfile, ESLint (0 warnings), Prettier --check over the repository, tsc,
# Vitest with coverage (thresholds.lines = 80 in apps/web/vitest.config.ts), next build with no
# secrets in the environment (S7). Coverage lands in apps/web/coverage/.
# shellcheck source=scripts/ci/lib.sh disable=SC1091  # followed with -x; single-file runs (pre-commit) cannot
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

if ! command -v pnpm >/dev/null 2>&1; then
  need corepack
  corepack enable
fi
need node pnpm

group "pnpm install"
pnpm install --frozen-lockfile
group "eslint"
pnpm --filter web lint
group "prettier --check"
pnpm exec prettier --check .
group "tsc"
pnpm --filter web typecheck
pnpm --filter @payrun/api-client typecheck
group "vitest (coverage gate 80% lines)"
pnpm --filter web test --coverage
group "next build (no secrets)"
# The build must not need runtime secrets (web LLD §8): run it with none of them set.
env -u INTERNAL_JWT_PRIVATE_KEY -u SESSION_SECRET -u REDIS_PASSWORD NEXT_TELEMETRY_DISABLED=1 \
  pnpm --filter web build
endgroup
