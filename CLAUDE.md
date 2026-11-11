# CLAUDE.md: working in the PayRun repo

PayRun is payroll for small and medium businesses: a Next.js 16 BFF and UI in front of FastAPI
services, Postgres (a schema per service), Redis Streams with an outbox, and a full OpenTelemetry
stack. Start with [README.md](README.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Repo map

| Path                  | What                                                                                                                                            |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `apps/web`            | Next.js pages and BFF (`src/app/api/**/route.ts`, `src/lib/`), Vitest (`tests/`), Playwright (`e2e/`)                                           |
| `packages/api-client` | TS types generated from identity and payroll `openapi.json` (don't edit `src/generated/`)                                                       |
| `services/<svc>`      | `identity`, `payroll`, `payout_worker`, `bank_stub`: `src/<svc>/`, `migrations/` (Alembic), `tests/{unit,contract,integration}`, `openapi.json` |
| `libs/pycommon`       | Shared Python: settings, logging, OTel, errors, JWT, health, DB, streams, limiter                                                               |
| `contracts/events`    | Event and DLQ JSON Schemas plus `examples/`                                                                                                     |
| `deploy/`             | `compose/`, `observability/`, `k8s/` (base, components, overlays), `docker/`                                                                    |
| `tests/platform`      | Tests of deploy assets and scripts; `tests/resilience`: fault tests on the stack                                                                |
| `load/`               | k6 scenarios                                                                                                                                    |
| `scripts/`            | `dev-env.sh`, `stack.sh`, `seed.py`, `mint_token.py`, `export_openapi.py`, `sync-k8s-assets.sh`, `ci/*.sh`                                      |
| `docs/`               | `plan/`, `specs/payrun-0.1/`, `adr/`, `runbooks/`, and the guides                                                                               |

## Commands for each gate

Setup: `uv sync --all-packages`, `corepack enable`, `pnpm install`, `scripts/dev-env.sh`.

| Gate                                             | Command                                                                                      |
| ------------------------------------------------ | -------------------------------------------------------------------------------------------- |
| Python member (lint, mypy, tests, coverage)      | `scripts/ci/python.sh <pycommon\|identity\|payroll\|payout_worker\|bank_stub\|platform>`     |
| Web (ESLint, Prettier, tsc, Vitest, int, build)  | `scripts/ci/web.sh`                                                                          |
| Contract (OpenAPI drift, oasdiff, event schemas) | `scripts/ci/contract.sh`                                                                     |
| Full e2e on the stack                            | `scripts/ci/e2e.sh` (`E2E_SUITE=skeleton` for the skeleton only)                             |
| k8s, promtool, amtool, Collector, compose config | `scripts/ci/manifests.sh`                                                                    |
| Images and Trivy                                 | `scripts/ci/images.sh`                                                                       |
| pip-audit, pnpm audit, gitleaks                  | `scripts/ci/security.sh`                                                                     |
| Formatting (root)                                | `corepack pnpm exec prettier --check .`                                                      |
| Regenerate contracts after an API change         | `uv run python scripts/export_openapi.py --all && pnpm --filter @payrun/api-client generate` |
| Stack                                            | `scripts/stack.sh up`, `seed`, `logs`, `down [-v]`, `reset`                                  |
| Nightly jobs                                     | `scripts/ci/nightly-{load,resilience,e2e,schemathesis,audit}.sh`                             |
| Alert drills (by hand)                           | `scripts/ci/alert_drills.sh [AlertName ...]`                                                 |

Details and per-suite pytest commands: [docs/TESTING.md](docs/TESTING.md).

## Conventions

- **Spec precedence:** the Contract decisions table (C) > the Spec resolutions table (R) >
  `requirements.md` > ADRs > designs > plan. The C and R tables are in
  [docs/specs/payrun-0.1/README.md](docs/specs/payrun-0.1/README.md). A new decision gets the next
  C number there, and the lower documents are updated to match.
- **Contracts first:** `services/*/openapi.json` (generated) and `contracts/events/*.json` change
  only with their producer and consumer tests.
- **Tests cite requirement IDs** (`PAY-…`, `PAY-NFR-…`) in a docstring or comment. A flake is a
  bug: no retries.
- **Unit tests have no network** (`--disable-socket --allow-unix-socket`); integration tests reach
  only Testcontainers.
- **Logging:** pycommon `log_event(logger, level, "<dotted.event.name>", **fields)`, lower-case and dotted, e.g.
  `payout.needs_operator`, `queue.message.failed`, `startup.check_failed`. Never interpolate
  values into the message; fields are redacted (`password`, `token`, `account`, `salary`,
  `amount`, …). No secrets, tokens, bank numbers or salaries in logs (PAY-NFR-07).
- **Metrics:** create counters with the final Prometheus name minus `_total` (the Collector adds
  it); histograms and gauges with unit `s` carry `_seconds`; gauges never use unit `"1"` (it
  becomes `_ratio`, spec C63). Add new series to `deploy/observability/metrics-catalog.json`.
  `scripts/ci/check_metrics.sh` lists the required ones.
- **Spans:** `<service>.<entity>.<action>`, e.g. `payroll.pay_run.calculate`, with attributes
  prefixed by the service (`payroll.pay_run_id`); each LLD lists its spans.
- **Errors:** `{error: {code, message, request_id, details?}}`; 422 is always `validation_error`
  with `details` `[{field, issue}]`; not-found is `<entity>_not_found`. A new code goes into the
  service, its OpenAPI responses, and the BFF catalogue `apps/web/src/lib/errors.ts`.
- **Money** is integer cents and rates are basis points (ADR 0006). Every query is scoped by the
  token's `cid`.
- **No test literals that look like credentials.** Generate passwords, keys and tokens at runtime
  (`secrets`, `crypto.randomBytes`, `scripts/mint_token.py`). Dev accounts use
  `@payrun.example.com`. `.env` is git-ignored; `.env.example` holds names and empty secrets only.
- **Before every commit, scan for secrets.** Run gitleaks through `scripts/ci/security.sh`
  (Docker; `GITLEAKS_BASE=<sha>` limits it to new commits). Where the optional local scanner
  `~/.claude/skills/feature-dev/scripts/secret_scan.py` is installed, also run it on the staged
  changes and commit only when it prints `matches: none`.
- **Shared files** (`libs/pycommon`, `contracts/`, `scripts/`, `deploy/`, `.github/`, root files,
  lock files) belong to the lead in multi-agent work.
- Kubernetes copies of shared assets are generated: edit the sources, then run
  `scripts/sync-k8s-assets.sh` (CI fails on a stale copy).

## Toolchain pins and quirks

- **Two-week rule:** `pyproject.toml` `[tool.uv] exclude-newer` and `pnpm-workspace.yaml`
  `minimumReleaseAge` only allow releases at least 14 days old.
- **pnpm 10.34.9**, not 12: corepack 0.31 can't load pnpm 12's ES-module entry point.
- **TypeScript 6.0.3**, not 7: typescript-eslint 8.72 requires TypeScript below 6.1.
- **Node:** CI uses 24 (`.nvmrc`); `engines` allows ≥ 22 locally. Node 23 can't load undici 8 or
  jsdom 30, so jsdom is pinned to 29.1.1.
- **Python 3.14** via uv (`uv python install`); uv 0.12.13 in CI.
- **`rm -f .coverage .coverage.*` before coverage runs**: stale data skews `--cov-append`. The
  gate (90 pycommon, 85 services) applies to the combined unit + integration report (spec C64).
- **Run pytest suites in separate processes** (unit, contract, integration): OTel metric globals
  leak between suites (spec C61).
- **mypy:** `tests/resilience` and `tests/platform` both have a top-level `conftest`, so mypy runs
  them separately (`scripts/ci/python.sh platform`).
- **SQLAlchemy instrumentation** uses `skip_dep_check=True` (`libs/pycommon/src/pycommon/otel.py`):
  instrumentation 0.66b1 gates on sqlalchemy < 2.1 but works on 2.1.6 (spec C61).
- **Testcontainers:** Python `testcontainers[postgres,redis]` 4.15.0 (locked in `uv.lock`); web
  `testcontainers` 12.0.1 pinned exactly, with its unused native deps (`ssh2`, `cpu-features`,
  `protobufjs`) in `ignoredBuiltDependencies`.
- **OpenSSL 3** is required by `scripts/dev-env.sh` (macOS `/usr/bin/openssl` is LibreSSL).
- **Always use `127.0.0.1`**, never `localhost`, for the dev web origin (spec C12).
- **Schemathesis damages seed data** (its stateful phase deletes runs): re-seed from a clean stack
  before anything that reads the seed (spec C70).
- Python services read `OTEL_EXPORTER_OTLP_TIMEOUT` in seconds (spec C67).

## Where the status lives

- Task status: [docs/specs/payrun-0.1/tasks.md](docs/specs/payrun-0.1/tasks.md) (Status column).
- Decisions and measurements: the C and R tables in the spec README (latest: C77).
- Plan status line and dated checks: [docs/plan/architecture-plan.md](docs/plan/architecture-plan.md) §18.
- Release notes: [CHANGELOG.md](CHANGELOG.md).
