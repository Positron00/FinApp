# Testing PayRun

Every gate runs the same way locally and in CI: each CI job calls one script in `scripts/ci/`.
The strategy is plan §12 and the test tables in each low-level design; every test cites the
requirement ID it proves (`PAY-…`) in its docstring or a comment.

Setup once: `uv sync --all-packages`, `corepack enable`, `pnpm install`, `scripts/dev-env.sh`.
Docker must be running (Testcontainers, the stack, and the pinned tool images).

## Python: lint, types, unit, contract, integration

```sh
scripts/ci/python.sh <member>   # pycommon | identity | payroll | payout_worker | bank_stub | platform
```

For one member this runs `ruff check`, `ruff format --check`, `mypy --strict`, then the test
suites in **separate pytest processes** (OTel metric globals would leak between suites in one
process, spec C61), with one combined coverage report (spec C64):

| Suite       | What it reaches                                           | Coverage step                                          |
| ----------- | --------------------------------------------------------- | ------------------------------------------------------ |
| unit        | No network: `--disable-socket --allow-unix-socket`        | starts the data, `--cov-fail-under=0`                  |
| contract    | Producer/consumer checks against `openapi.json` and `contracts/events/` (services only) | `--cov-append --cov-fail-under=0` |
| integration | Real Postgres and Redis via Testcontainers                | `--cov-append --cov-fail-under=<gate>`                 |

**Gates** (`fail_under` in each member's `pyproject.toml`, applied to the combined unit +
integration report): **90%** for `libs/pycommon`, **85%** for each service; branch coverage on.

The same steps by hand, for example payroll (always delete stale coverage data first, or
`--cov-append` merges it):

```sh
cd services/payroll
rm -f .coverage .coverage.*
uv run pytest tests/unit --disable-socket --allow-unix-socket --cov=payroll --cov-branch --cov-fail-under=0 --cov-report=
uv run pytest tests/contract --cov=payroll --cov-branch --cov-append --cov-fail-under=0 --cov-report=
uv run pytest tests/integration --cov=payroll --cov-branch --cov-append --cov-fail-under=85 --cov-report=term
```

Use `--cov=pycommon` and `--cov-fail-under=90` in `libs/pycommon` (it has no contract suite), and
`identity`, `payout_worker` or `bank_stub` in the other service directories.

Member `platform` lints and type-checks `scripts/`, `tests/` and `deploy/observability/alert-sink`
(mypy runs `tests/resilience` separately, because it and `tests/platform` both have a top-level
`conftest` module), then runs `tests/platform` without a coverage gate:

```sh
uv run mypy --strict scripts tests/platform deploy/observability/alert-sink
uv run mypy --strict tests/resilience
uv run pytest tests/platform --ignore=tests/platform/test_seed.py -p no:cacheprovider
```

`tests/platform` checks the deploy assets and scripts: bootstrap SQL grants, Redis ACLs, Compose,
Kubernetes policy, dashboards against the metric catalogue, rule metadata, alert-sink,
Toxiproxy config, `dev-env.sh`, `mint_token.py`, `sync-k8s-assets.sh` and `soak_resources.py`.
`test_seed.py` needs the running stack and is run by `e2e.sh` (`-m stack`).

## Web: lint, types, Vitest, int, build

```sh
scripts/ci/web.sh
```

It runs `pnpm install --frozen-lockfile`, ESLint with 0 warnings, Prettier `--check` over the
repository, `tsc` for `web` and `@payrun/api-client`, then:

| Step         | Command                                   | Notes                                                        |
| ------------ | ----------------------------------------- | ------------------------------------------------------------ |
| Vitest       | `pnpm --filter web test --coverage`       | `apps/web/tests/**`; gate `thresholds.lines = 80` in `apps/web/vitest.config.ts` |
| Vitest int   | `pnpm --filter web int`                   | `apps/web/tests/**/*.int.test.ts` (`tests/lib/session.int.test.ts`, `rate-limit.int.test.ts`) on a Testcontainers Redis with the production ACLs (sessions, rate limits) |
| Build        | `pnpm --filter web build`                 | Run with the secrets unset: the build must not need them     |

## Contract: drift, breaking changes, event schemas

```sh
scripts/ci/contract.sh [base-ref]   # default origin/main
```

1. **Drift:** `uv run python scripts/export_openapi.py --all` and
   `pnpm --filter @payrun/api-client generate`, then `git diff --exit-code` on
   `services/*/openapi.json` and `packages/api-client`. To fix drift, run those two commands and
   commit. `export_openapi.py --check` only reports.
2. **Breaking changes:** `oasdiff breaking` (pinned image) of each spec against the base ref,
   failing on `ERR`.
3. **Event schemas:** `check-jsonschema` validates `contracts/events/*.json` against the
   metaschema and every example against its schema, and checks the dev `COMPANY_ID` (spec C28).

## End to end on the stack

```sh
scripts/ci/e2e.sh                          # the full suite (CI default)
E2E_SUITE=skeleton scripts/ci/e2e.sh       # the walking skeleton only
E2E_KEEP_STACK=1 scripts/ci/e2e.sh         # leave the stack running afterwards
```

`e2e.sh` builds the images, runs `scripts/stack.sh up --check-startup`, seeds (full:
`seed.py`, then `seed.py --employees 500`), and runs, in this order (Schemathesis's stateful phase
damages seed data, so it comes after everything that reads it; spec C70):

1. **Playwright** (`apps/web/e2e/`, Chromium, one worker):
   - `@skeleton`: `skeleton.spec.ts` (sign in, calculate, approve as the approver, `paid`, trace);
   - `@journeys`: J1–J5 (`access`, `employees`, `pay-run`, `approve-pay`, `recover`), `role-change`
     and `errors`;
   - `@trace`: one trace across every hop of J4 (`trace.spec.ts`);
   - `@a11y`: axe checks (`a11y.spec.ts`).

   By hand on a running, seeded stack:
   `pnpm --filter web exec playwright test --grep "@journeys|@trace|@a11y"` (or `--grep @skeleton`).
2. **Seed tests:** `uv run pytest tests/platform/test_seed.py -m stack` (PAY-609).
3. **k6 smoke:** `SEED_JSON=ci-logs/seed.json scripts/ci/k6.sh load/smoke.js`.
4. **Schemathesis:** `scripts/ci/schemathesis.sh [identity|payroll|bank_stub]` against the
   published service ports, with every check except `positive_data_acceptance` (spec C65), 50
   examples per operation, and a fresh internal JWT per request from
   `scripts/ci/schemathesis_auth.py` (bank-stub: its API key). Reports in `ci-logs/schemathesis/`.
5. **Fault smoke:** `uv run pytest tests/resilience -m smoke` (slow DB and identity down).
6. **Metric series:** `scripts/ci/check_metrics.sh` (18 required series in Prometheus).

A skipped seed test or fault smoke test fails the job. The stack is always torn down with
`down -v`; on failure the Compose logs are in `ci-logs/compose.log`.

## Load (k6)

```sh
scripts/ci/k6.sh load/smoke.js        # any script in load/; summary in load/results/<script>.json
scripts/ci/nightly-load.sh            # full, soak and settlement on a fresh stack with 500 employees
LOAD_SCENARIOS="settlement" FULL_HOLD=2m SOAK_DURATION=10m scripts/ci/nightly-load.sh   # shorter local check
```

| Script               | Shape                                                    | Pass criteria                                              |
| -------------------- | -------------------------------------------------------- | ---------------------------------------------------------- |
| `load/smoke.js`      | 5 VUs for 30 s as the load user, read-only pages (PR e2e) | errors < 1%, p95 < 300 ms                                  |
| `load/full.js`       | Ramping arrival rate to 30 operations/s, held 10 min     | errors < 1%, p95 < 300 ms (simple), < 1.5 s (calculate)    |
| `load/soak.js`       | 5 operations/s for 30 min                                | same, plus no p95 drift (last vs first 5 min within 20%), Python RSS growth < 5%, no growing consumer lag (`scripts/ci/soak_resources.py`) |
| `load/settlement.js` | Five 500-employee runs, approved as the approver         | settlement p95 ≤ 60 s, cross-checked with `payroll_payout_settlement_seconds` |

`k6.sh` uses a local k6 if installed, else the pinned `grafana/k6` image on the Compose network.

## Resilience (fault injection)

`tests/resilience/` runs against the Compose stack with Toxiproxy (the `test` profile), one test
per failure-table row (HLD §4): worker crash before and after the bank call, bank down then up,
bank timeout after settling, bank-stub restart, payroll down, Redis down, Redis `FLUSHALL`, Redis
restart, Postgres down, slow DB, identity down and SMTP down.

```sh
uv run pytest tests/resilience -m smoke     # PR subset (2 tests), on a running stack
scripts/ci/nightly-resilience.sh            # all 13 tests on a fresh stack; any skip fails
```

Payout timing stays at production values (spec C53), so the bank-down, payroll-down and
`FLUSHALL` tests each wait out a re-publish (about 8 minutes).

## Deploy assets and security

| Gate                                  | Command                     | What it checks                                                     |
| ------------------------------------- | --------------------------- | ------------------------------------------------------------------ |
| Kubernetes and observability configs  | `scripts/ci/manifests.sh`   | `sync-k8s-assets.sh` copies are current; `kustomize build` of dev, uat and prod piped to **kubeconform** `-strict` (with the vendored ExternalSecret schemas); `test_k8s_policy.py`; **promtool** `check config`, `check rules`, `test rules` (Compose rules and `PodRestarts`); **amtool** `check-config`; `otelcol-contrib validate`; `docker compose config -q` |
| Images                                | `scripts/ci/images.sh`      | `.trivyignore` policy (reason, owner, unexpired, listed in `SECURITY_DEPENDENCIES.md`); `docker buildx bake` for linux/amd64; **Trivy** HIGH/CRITICAL per image |
| Dependencies and secrets              | `scripts/ci/security.sh`    | `pip-audit --strict`; `pnpm audit --audit-level high`; **gitleaks** over `<base>..HEAD` (`GITLEAKS_BASE`) |

## Alert drills

`scripts/ci/alert_drills.sh` fires each paging alert on the live stack and checks delivery,
runbook link and dashboard; it is run by hand, not in CI. See
[RUNBOOK.md](RUNBOOK.md#alert-drills).

## PR CI vs nightly

| Workflow                                    | Job                 | Script                                   | Timeout |
| ------------------------------------------- | ------------------- | ---------------------------------------- | ------- |
| `ci.yml` (pull requests, pushes to `main`)  | `python` (matrix of 6 members) | `scripts/ci/python.sh <member>` | 20 min  |
|                                             | `web`               | `scripts/ci/web.sh`                      | 15 min  |
|                                             | `contract`          | `scripts/ci/contract.sh`                 | 15 min  |
|                                             | `e2e`               | `scripts/ci/e2e.sh` (full suite)         | 40 min  |
|                                             | `images`            | `scripts/ci/images.sh`                   | 30 min  |
|                                             | `manifests`         | `scripts/ci/manifests.sh`                | 10 min  |
|                                             | `security`          | `scripts/ci/security.sh`                 | 10 min  |
| `nightly.yml` (03:17 UTC, or `workflow_dispatch`) | `load`        | `scripts/ci/nightly-load.sh`             | 100 min |
|                                             | `resilience`        | `scripts/ci/nightly-resilience.sh`       | 60 min  |
|                                             | `e2e-repeat`        | `scripts/ci/nightly-e2e.sh` (full e2e 3 times) | 120 min |
|                                             | `schemathesis-deep` | `scripts/ci/nightly-schemathesis.sh` (500 examples per operation) | 45 min |
|                                             | `audit`             | `scripts/ci/nightly-audit.sh` (audits, fresh Trivy DB) | 20 min |
|                                             | (on failure)        | `scripts/ci/report_failure.sh` opens or updates a `nightly-failure` issue | 10 min |

Not in CI: `scripts/ci/alert_drills.sh` (by hand on the stack).

## Measured results (2026-11-11)

Measured locally on the development machine (an Apple-silicon Mac) with the Compose stack.

| What                                  | Result                                                                                       |
| ------------------------------------- | -------------------------------------------------------------------------------------------- |
| Full local e2e (`scripts/ci/e2e.sh`)  | Green: 27 Playwright tests, 8 seed tests, Schemathesis identity 1085, payroll 2159 and bank-stub 172 generated cases all passing, 18 of 18 metric series present |
| Settlement, 500-employee run          | p95 5.7 s (SLO ≤ 60 s; `load/settlement.js`)                                                 |
| k6 smoke                              | p95 about 30 ms                                                                              |
| Service startup                       | about 1.2 s per service from container start to ready (limit 20 s, PAY-NFR-15)               |
| Nightly resilience suite              | 13 tests passed in 27.6 min (job timeout 60 min)                                             |
| Alert drills                          | All 6 paging alerts delivered to alert-sink `/page`; timings in spec C75 ([spec README](specs/payrun-0.1/README.md)) |

### Nightly load and soak

<!-- NIGHTLY_RESULTS -->
