# Low-level design: platform

Owner: lead. Sources: [architecture plan](../../plan/architecture-plan.md) v1.3 (§2.2, §6, §8, §11–§13, §17), ADRs [0007](../../adr/0007-data-stores.md), [0008](../../adr/0008-secrets.md), [0010](../../adr/0010-alert-receivers.md), [0011](../../adr/0011-egress.md), [requirements](requirements.md) (PAY-600–609, PAY-NFR-01–15), [high-level design](design-high-level.md), and the spec README's "Spec resolutions" (R) and "Contract decisions" (C) tables, which this body follows.

Scope: `libs/pycommon`, the repo scaffold, the DB bootstrap SQL, Redis config and ACLs, `scripts/`, Docker Compose, the observability configs, Kubernetes, images and CI. Service-specific tables, endpoints and pages are in the identity, payroll, payout-worker, bank-stub and web low-level designs; this file gives them the shared pieces they build on.

**Platform decisions taken here** (each justified where it appears):

| # | Decision | Where |
|---|---|---|
| D1 | **Env names (C1):** the pycommon `BaseServiceSettings` names are the contract for every Python service, unprefixed: `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_SCHEMA`, `REDIS_HOST`, `REDIS_PORT`, `REDIS_USERNAME`, `REDIS_PASSWORD`, plus the other §4 keys (`DB_STATEMENT_TIMEOUT_MS`, `LOG_LEVEL`, `DEBUG`, `DOCS_ENABLED`, …). Migrate containers set the same keys with migrator credentials. Service-only keys keep a service prefix (`IDENTITY_`, `PAYROLL_`, `PAYOUT_`, `BANK_STUB_`), except keys the plan or requirements already name (`BANK_BASE_URL`, `AUTH_LOGIN_LIMIT_*`, `INVITE_TTL_H`, …). The SMTP keys (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`) are unprefixed infrastructure keys like `DB_*` (C49). No service reads `DATABASE_URL` or `REDIS_URL` | §4 |
| D2 | DB role passwords are injected with psql `\getenv` + `format(%L)` + `\gexec`, so one `.sql` artifact runs unchanged in Compose, Testcontainers and the Kubernetes Job | §2.1 |
| D3 | The Redis ACL file is generated from env at container start by a POSIX `sh` entrypoint, with SHA-256 hashed passwords, into a tmpfs | §2.2 |
| D4 | One shared Python Dockerfile (`deploy/docker/python-service.Dockerfile`) with build args; every Python process (including the worker) is an ASGI app served by uvicorn | §10 |
| D5 | alert-sink is a ~100-line stdlib Python script run in the pinned Python base image: no extra image to pin or scan | §8.5 |
| D6 | **No Lua in Redis (C3):** rate limiting is a fixed-window counter (`INCR` + `EXPIRE … NX` in `MULTI`), and lock levels use `SET NX EX`, so every ACL user keeps `-@scripting` | §3.11, §2.2 |
| D7 | **Gauges are computed at metric collection (R26, C18):** OTel observable-gauge callbacks run on the metric reader's thread every export interval (15 s). pycommon offers only the callback helper (`observable_gauge`); each service owns its queries (payroll LLD §7.2 is authoritative for payroll's gauges, `outbox_*` included) | §3.3 |
| D8 | **Kubernetes copies of shared assets (R32, C36):** `scripts/sync-k8s-assets.sh` copies the bootstrap SQL (and the Redis and Collector configs) under `deploy/k8s/`, and CI fails if the copy differs. Kustomize runs with no load-restrictor flag | §3.15, §9 |

## 1. Modules and repo layout

### 1.1 Tree

```text
FinApp/
  .editorconfig  .gitignore  .dockerignore  .pre-commit-config.yaml  .gitleaks.toml
  .python-version            # 3.14 (see §10.3)
  .nvmrc                     # 24
  .trivyignore               # 17 accepted base-image CVEs (C66); each needs reason, owner, exp:YYYY-MM-DD
  .env.example               # documents the keys scripts/dev-env.sh writes, one comment each; no values for
                             # secrets. Other keys (§4) take their code defaults
  CLAUDE.md                  # agent and contributor notes
  pyproject.toml  uv.lock    # uv workspace root (§1.2)
  package.json  pnpm-workspace.yaml  pnpm-lock.yaml
  CHANGELOG.md  README.md  SECURITY_DEPENDENCIES.md
  apps/web/                  # web LLD; Dockerfile here (§10.2)
  packages/api-client/       # generated TS types (web LLD)
  libs/pycommon/
    pyproject.toml
    src/pycommon/
      __init__.py  py.typed
      settings.py  logging.py  otel.py  errors.py  auth.py  health.py  db.py
      redis_client.py  streams.py  http.py  ratelimit.py  startup_checks.py
      maintenance.py  app.py  serve.py  testing.py
    tests/
      unit/  integration/  conftest.py
  services/
    identity/  payroll/  payout_worker/  bank_stub/
      pyproject.toml  alembic.ini  migrations/  openapi.json (not for payout_worker)
      src/<pkg>/{main.py, settings.py, api/, domain/, db/, events/, clients/}
      tests/{unit,integration,contract}/
  contracts/events/payout.requested.v1.json
  contracts/events/payout.requested.dlq.v1.json      # DLQ entry fields (C21)
  contracts/events/examples/payout.requested.v1.*.json   # company_id = the dev COMPANY_ID (C28)
  deploy/
    docker/python-service.Dockerfile  docker/bake.hcl
    compose/
      compose.yaml  compose.test.yaml  toxiproxy.json
      init/
        00-database.sql  10-identity.sql  20-payroll.sql  30-payouts.sql  40-bank-stub.sql  90-monitoring.sql
        lib/service_schema.psql
        lib/run-init.sh       # loop used by the Kubernetes Job (§9.3); in lib/ because the Postgres entrypoint runs any *.sh in initdb.d
      redis/redis.conf  redis/entrypoint.sh
    observability/
      collector.yaml  collector.prod.yaml
      prometheus/prometheus.yml
      prometheus/rules/recording.yml  prometheus/rules/alerts.yml
      prometheus/tests/slo_test.yml   prometheus/tests/alerts_test.yml
      alertmanager.yml
      alert-sink/alert_sink.py
      tempo.yaml  loki.yaml
      grafana/provisioning/datasources/datasources.yaml
      grafana/provisioning/dashboards/dashboards.yaml
      grafana/dashboards/{service-overview,journeys,queues,bank,platform}.json
    k8s/
      base/  components/  overlays/{dev,uat,prod}/  schemas/   # §9; synced copies marked in §9.1
      observability/alerts-kubernetes.yml  observability/alerts-kubernetes_test.yml   # PodRestarts (R28)
  load/                      # k6 (smoke.js, full.js, soak.js, settlement.js)
  tests/
    resilience/              # Toxiproxy fault tests
    platform/                # tests for deploy assets and scripts (§6.2)
  scripts/
    dev-env.sh  stack.sh  export_openapi.py  seed.py  mint_token.py  sync-k8s-assets.sh
    ci/{lib.sh, python.sh, web.sh, contract.sh, e2e.sh, images.sh, manifests.sh, security.sh,
        check_metrics.sh, k6.sh, schemathesis.sh, schemathesis.toml, schemathesis_auth.py,
        soak_resources.py, alert_drills.sh, nightly-load.sh, nightly-resilience.sh, nightly-e2e.sh,
        nightly-schemathesis.sh, nightly-audit.sh, report_failure.sh}
  docs/{plan,specs,adr,runbooks}/  docs/{ARCHITECTURE,RUNBOOK,TESTING,API}.md
    runbooks/<AlertName>.md  # one per alert in §8.3 plus PodRestarts (18 files)
  .github/workflows/ci.yml  .github/workflows/nightly.yml  .github/dependabot.yml
```

### 1.2 Workspace files

**`pyproject.toml` (root):**

```toml
[project]
name = "payrun-workspace"
version = "0.1.0"
requires-python = ">=3.14,<3.15"

[tool.uv]
package = false
default-groups = ["dev"]
exclude-newer = "2026-10-28T00:00:00Z"   # the two-week rule, enforced by the resolver (C58)

[tool.uv.workspace]
members = ["libs/*", "services/*"]

[tool.uv.sources]
pycommon = { workspace = true }

[dependency-groups]
dev = [
  "ruff", "mypy", "pytest", "pytest-asyncio", "pytest-cov", "pytest-socket", "pytest-timeout",
  "respx", "time-machine", "hypothesis", "polyfactory", "testcontainers[postgres,redis]",
  "schemathesis", "pip-audit", "httpx", "check-jsonschema", "jsonschema", "pre-commit",
]   # exact versions are in uv.lock; lower bounds set at scaffold time

[tool.ruff]
line-length = 100
target-version = "py314"
extend-exclude = ["apps", "packages", "node_modules"]   # first-party detection comes from the workspace install

[tool.ruff.lint]
select = ["E", "W", "F", "I", "B", "UP", "S", "ASYNC", "RUF", "SIM", "PT", "N", "C4", "DTZ", "T20", "TID", "PL", "PERF"]
ignore = ["PLR0913", "PLR2004"]

[tool.ruff.lint.per-file-ignores]
"**/tests/**" = ["S101", "S105", "S106", "PLR2004", "S311", "S603", "S607", "S608"]   # asserts, fixtures, subprocess and SQL in tests
"scripts/**" = ["T20"]

[tool.ruff.lint.flake8-tidy-imports.banned-api]
"requests".msg = "use httpx via pycommon.http"
"datetime.datetime.utcnow".msg = "use datetime.now(UTC)"
"logging.basicConfig".msg = "use pycommon.logging.setup_logging"

[tool.ruff.format]
quote-style = "double"

[tool.mypy]
strict = true
python_version = "3.14"
plugins = ["pydantic.mypy"]
warn_unreachable = true

[[tool.mypy.overrides]]
module = ["testcontainers.*", "schemathesis.*"]
ignore_missing_imports = true

[tool.coverage.run]
branch = true
parallel = true

[tool.coverage.report]
show_missing = true
skip_covered = true
exclude_also = ["if TYPE_CHECKING:", "raise NotImplementedError", "@overload"]

[tool.pytest.ini_options]   # suites without their own config: tests/platform, tests/resilience
addopts = "--strict-markers"
asyncio_default_fixture_loop_scope = "function"
markers = ["integration: needs Docker (Testcontainers)", "smoke: PR fault smoke tests"]
timeout = 180
```

**Member `pyproject.toml`** (pycommon shown; services the same shape with `dependencies = ["pycommon", …]`):

```toml
[project]
name = "pycommon"
version = "0.1.0"
requires-python = ">=3.14,<3.15"
dependencies = [
  "fastapi", "uvicorn[standard]", "pydantic", "pydantic-settings", "sqlalchemy[asyncio]", "asyncpg",
  "alembic", "redis", "httpx", "pyjwt[crypto]", "cryptography",
  "opentelemetry-api", "opentelemetry-sdk", "opentelemetry-exporter-otlp-proto-grpc",
  "opentelemetry-instrumentation-fastapi", "opentelemetry-instrumentation-httpx",
  "opentelemetry-instrumentation-sqlalchemy", "opentelemetry-instrumentation-redis",
  "opentelemetry-instrumentation-system-metrics", "opentelemetry-instrumentation-logging",
]
[project.optional-dependencies]
testing = ["pytest", "testcontainers[postgres,redis]", "respx", "time-machine"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.pytest.ini_options]
addopts = "--strict-markers"
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
timeout = 60
testpaths = ["tests"]
markers = ["integration: needs Docker (Testcontainers)"]

# The coverage gate lives in each member, read when the gate runs from the member directory (C63):
# fail_under = 90 for pycommon, 85 for each service (PAY-NFR-12).
[tool.coverage.run]
branch = true
parallel = true
source = ["pycommon"]

[tool.coverage.report]
fail_under = 90
show_missing = true
skip_covered = true
exclude_also = ["if TYPE_CHECKING:", "raise NotImplementedError", "@overload"]
```

pytest-socket loads from its entry point, so gate commands pass `--disable-socket --allow-unix-socket`
and never `-p pytest_socket` (C58).

**`pnpm-workspace.yaml`:**

```yaml
packages:
  - "apps/*"
  - "packages/*"
minimumReleaseAge: 20160   # minutes: the two-week rule (C58)
```

Root `package.json`: `"private": true`, `"packageManager": "pnpm@<pinned>"` (corepack), `"engines": {"node": ">=22"}` (CI pins Node 24 LTS through `.nvmrc`; README toolchain notes), scripts `lint`, `format:check` (`prettier --check .`), `typecheck`, `test` delegating with `pnpm -r`.

**`.editorconfig`:**

```ini
root = true
[*]
charset = utf-8
end_of_line = lf
insert_final_newline = true
trim_trailing_whitespace = true
indent_style = space
indent_size = 2
[*.py]
indent_size = 4
[*.md]
trim_trailing_whitespace = false
[Makefile]
indent_style = tab
```

**`.pre-commit-config.yaml`** (revs pinned at scaffold time, `pre-commit autoupdate` monthly via Dependabot-style PR):

```yaml
repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: <pinned>
    hooks:
      - id: check-yaml
        args: [--allow-multiple-documents]
        exclude: ^deploy/k8s/          # Kustomize patches aren't plain YAML documents; kubeconform checks them
      - id: check-json
        exclude: ^apps/web/tsconfig.*\.json$
      - id: end-of-file-fixer
      - id: trailing-whitespace
      - id: check-merge-conflict
      - id: check-added-large-files
        args: [--maxkb=500]
      - id: detect-private-key
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: <pinned>
    hooks:
      - id: ruff-check
        args: [--fix]
      - id: ruff-format
  - repo: https://github.com/shellcheck-py/shellcheck-py
    rev: <pinned>
    hooks:
      - id: shellcheck
  - repo: https://github.com/gitleaks/gitleaks
    rev: <pinned>
    hooks:
      - id: gitleaks          # scans staged changes: the "pre-commit secret scan" of plan §15
  - repo: local
    hooks:
      - id: prettier
        name: prettier
        entry: pnpm exec prettier --check --ignore-unknown
        language: system
        types_or: [javascript, ts, tsx, json, yaml, css]
```

mypy and ESLint are not pre-commit hooks (too slow per commit, and ESLint needs the web's type
information); CI runs both. Markdown isn't Prettier-checked: `.prettierignore` excludes `docs/`, whose
tables Prettier would re-wrap.

**`.gitignore` additions** (the existing file only anchors `/node_modules` and `/.next/` at the root, which misses `apps/web`):

```gitignore
node_modules/
.next/
.venv/
__pycache__/
*.py[cod]
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
.coverage.*
coverage.xml
htmlcov/
playwright-report/
test-results/
blob-report/
load/results/
ci-logs/
deploy/k8s/overlays/dev/secrets.env
.env.*
!.env.example
```

**`.dockerignore`** (build context is the repo root):

```text
.git
.github
**/node_modules
**/.venv
**/__pycache__
**/.pytest_cache
**/.mypy_cache
**/.ruff_cache
**/tests
**/.next
**/coverage
**/playwright-report
**/test-results
docs
load
deploy/k8s
deploy/observability
.env
.env.*
**/*.pem
ci-logs
```

### 1.3 Module table

| Module (path) | New / changed | Responsibility |
|---|---|---|
| `libs/pycommon/src/pycommon/settings.py` | New | `BaseServiceSettings`, `Env`, `load_settings`, secret and keyset parsing |
| `…/logging.py` | New | JSON formatter, `redact`, `log_event`, `request_id_var`, `user_text` |
| `…/otel.py` | New | Tracer, meter and logger providers; instrumentors; histogram buckets; `observable_gauge` (collection-time callback helper); traceparent helpers; event-loop lag |
| `…/errors.py` | New | `AppError` hierarchy, error body, exception handlers |
| `…/auth.py` | New | Issuer-bound `Keyset`, `TokenVerifier`, `TokenSigner`, `Principal` (with `name`), FastAPI auth dependencies (401/403 rule of C30) |
| `…/health.py` | New | `/livez`, `/readyz`, DB, Redis and migration-head checks |
| `…/db.py` | New | Async engine with timeouts, sessions, error mapping, `TimestampMixin`, `new_id`, advisory-lock keys, Alembic env helper |
| `…/redis_client.py` | New | `create_redis` per ACL user; the same factory with a longer socket timeout gives the worker its blocking-read client |
| `…/streams.py` | New | `Envelope` (`traceparent` required), `StreamProducer.publish` (the three stream fields of R7), `RelayLoop` over an `OutboxRepository` protocol. No tables (C22) |
| `…/http.py` | New | `ProviderPolicy`, `CircuitBreaker`, `Guard`, `ResilientClient`, typed errors |
| `…/ratelimit.py` | New | Redis fixed-window limiter, fail-open / fail-closed, caller-limit dependency with `exempt_subs` |
| `…/startup_checks.py` | New | Common and service-specific startup checks, timing invariants; exit 2 with `startup.check_failed` (C29) |
| `…/maintenance.py` | New | Advisory-lock periodic runner; services supply the tasks |
| `…/app.py` | New | `create_app(settings, …)`, `AppContext`, middleware, lifespan |
| `…/serve.py` | New | `python -m pycommon.serve`: runs uvicorn on `$PYCOMMON_APP`, no access log |
| `…/testing.py` | New | Pytest plugin: Postgres-with-bootstrap and Redis-with-ACL containers, token factory, in-memory OTel, fake clock |
| `deploy/compose/init/*` | New | Schemas, roles, default privileges, exporter role (PAY-606) |
| `deploy/compose/redis/*` | New | Redis config and ACL generation (PAY-607) |
| `deploy/compose/compose*.yaml`, `toxiproxy.json` | New | Local and CI stack (PAY-NFR-13) |
| `deploy/observability/*` | New | Collector, Prometheus, rules, Alertmanager, alert-sink, Tempo, Loki, Grafana (PAY-NFR-04, 10, 11) |
| `deploy/k8s/*` | New | Kustomize base, components, overlays (PAY-NFR-13) |
| `contracts/events/payout.requested.dlq.v1.json` | New | DLQ entry schema (C21) |
| `deploy/docker/python-service.Dockerfile`, `apps/web/Dockerfile` | New | Images |
| `scripts/*` | New | dev-env, stack, OpenAPI export, seed, token minting, k8s asset sync, CI scripts |
| `.github/workflows/*` | New | ci.yml, nightly.yml (PAY-NFR-12) |

## 2. Data models

### 2.1 DB bootstrap SQL (`deploy/compose/init/`; PAY-606)

**Password injection: psql `\getenv` (chosen).** Each file reads the role passwords from the process
environment with `\getenv` (psql ≥ 15; every psql here is 16) and builds the DDL with
`format('… PASSWORD %L', :'pw')` executed by `\gexec`.

- **Why not a shell wrapper:** a wrapper interpolates passwords into SQL text through a heredoc, so a
  password containing `'` or `$` breaks quoting or injects SQL; it also needs `sh` in every runner.
  With `\getenv`, `%L` does the quoting inside Postgres, and the same `.sql` files run unchanged in:
  1. Compose: mounted at `/docker-entrypoint-initdb.d` (the entrypoint runs `*.sql` with psql and ignores the `lib/` directory);
  2. Testcontainers: `pycommon.testing` mounts the same directory into `postgres:16-alpine` with the passwords as container env;
  3. the Kubernetes `db-bootstrap` Job: `lib/run-init.sh` loops over the files (it must live in `lib/`: the Postgres image entrypoint executes every `*.sh` in `docker-entrypoint-initdb.d`, found in S3) with `psql -v ON_ERROR_STOP=1 -f`. Kustomize can't read files outside its root, so `scripts/sync-k8s-assets.sh` copies the directory byte for byte to `deploy/k8s/base/bootstrap/init/` and CI fails on any difference (R32, C36). `deploy/compose/init/` stays the only source that is edited.
- **Missing variable:** the template checks `:{?var}` and that the value isn't empty, and raises, so the run fails with the variable's name and never with its value.
- **Idempotent:** roles are created only if missing, then `ALTER ROLE … PASSWORD` keeps them in sync, so the Job can be re-run to rotate passwords. Schema and grants use `IF NOT EXISTS` or are naturally repeatable.
- **No logging of passwords:** the server keeps `log_statement = none` (the default); `compose.yaml` doesn't change it. The test `test_bootstrap_sql.py` asserts this setting.
- **Managed Postgres** (uat, prod: the admin is not a superuser): in PG 16 a `CREATEROLE` admin has ADMIN on roles it creates but can't `SET ROLE` to them, which `CREATE SCHEMA … AUTHORIZATION` and `ALTER DEFAULT PRIVILEGES FOR ROLE` need. The template grants each migrator to `current_user WITH SET TRUE, INHERIT FALSE` when `current_user` is not a superuser.

**Roles** (database `payrun`; names per C2):

| Schema | Migrator role (owns schema, runs Alembic) | Runtime role (DML only) | Password env vars |
|---|---|---|---|
| `identity` | `identity_migrator` | `identity_app` | `IDENTITY_DB_MIGRATOR_PASSWORD`, `IDENTITY_DB_PASSWORD` |
| `payroll` | `payroll_migrator` | `payroll_app` | `PAYROLL_DB_MIGRATOR_PASSWORD`, `PAYROLL_DB_PASSWORD` |
| `payouts` | `payouts_migrator` | `payouts_app` | `PAYOUTS_DB_MIGRATOR_PASSWORD`, `PAYOUTS_DB_PASSWORD` |
| `bank_stub` (dev, uat only) | `bank_stub_migrator` | `bank_stub_app` | `BANK_STUB_DB_MIGRATOR_PASSWORD`, `BANK_STUB_DB_PASSWORD` |
| — | — | `postgres_exporter` (`pg_monitor`) | `POSTGRES_EXPORTER_PASSWORD` |

Runtime roles are named `<schema>_app` rather than `<schema>`, so `search_path = "$user", …` can't silently resolve to a same-named schema.

**`00-database.sql`:**

```sql
-- Runs as the bootstrap admin (POSTGRES_USER in Compose, the managed admin in uat/prod).
REVOKE ALL ON DATABASE payrun FROM PUBLIC;            -- no CONNECT/TEMP for unknown roles
REVOKE ALL ON SCHEMA public FROM PUBLIC;              -- nothing lives in public
```

**`lib/service_schema.psql`** (included; variables `schema`, `migrator`, `runtime`, `migrator_env`, `runtime_env`):

```sql
\getenv migrator_pw :migrator_env
\getenv runtime_pw :runtime_env
\if :{?migrator_pw}
\else
  \echo 'bootstrap: missing env' :migrator_env
  DO $$ BEGIN RAISE EXCEPTION 'bootstrap: required password env var is not set'; END $$;
\endif
\if :{?runtime_pw}
\else
  \echo 'bootstrap: missing env' :runtime_env
  DO $$ BEGIN RAISE EXCEPTION 'bootstrap: required password env var is not set'; END $$;
\endif
-- An empty value counts as missing.
SELECT (:'migrator_pw' = '') AS migrator_pw_empty, (:'runtime_pw' = '') AS runtime_pw_empty \gset
\if :migrator_pw_empty
  \echo 'bootstrap: missing env' :migrator_env
  DO $$ BEGIN RAISE EXCEPTION 'bootstrap: required password env var is empty'; END $$;
\endif
\if :runtime_pw_empty
  \echo 'bootstrap: missing env' :runtime_env
  DO $$ BEGIN RAISE EXCEPTION 'bootstrap: required password env var is empty'; END $$;
\endif
-- \echo prints the variable holding the env var's NAME, never a value

SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT', :'migrator')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'migrator') \gexec
SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT CONNECTION LIMIT 60', :'runtime')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'runtime') \gexec
SELECT format('ALTER ROLE %I PASSWORD %L', :'migrator', :'migrator_pw') \gexec
SELECT format('ALTER ROLE %I PASSWORD %L', :'runtime', :'runtime_pw') \gexec

SELECT NOT rolsuper AS need_set_grant FROM pg_roles WHERE rolname = current_user \gset
\if :need_set_grant
  SELECT format('GRANT %I TO current_user WITH SET TRUE, INHERIT FALSE', :'migrator') \gexec
\endif

SELECT format('GRANT CONNECT ON DATABASE payrun TO %I, %I', :'migrator', :'runtime') \gexec
SELECT format('CREATE SCHEMA IF NOT EXISTS %I AUTHORIZATION %I', :'schema', :'migrator') \gexec
SELECT format('ALTER SCHEMA %I OWNER TO %I', :'schema', :'migrator') \gexec
SELECT format('REVOKE ALL ON SCHEMA %I FROM PUBLIC', :'schema') \gexec
SELECT format('GRANT USAGE ON SCHEMA %I TO %I', :'schema', :'runtime') \gexec   -- USAGE only, no CREATE

SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I',
              :'migrator', :'schema', :'runtime') \gexec
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT USAGE, SELECT ON SEQUENCES TO %I',
              :'migrator', :'schema', :'runtime') \gexec
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT EXECUTE ON FUNCTIONS TO %I',
              :'migrator', :'schema', :'runtime') \gexec
-- tables that already exist (re-run after migrations) get the same grants
SELECT format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO %I', :'schema', :'runtime') \gexec
SELECT format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO %I', :'schema', :'runtime') \gexec
-- C63: the grant above would give the runtime role write access to the Alembic version table
-- again (run_alembic_env revokes it after each upgrade), so revoke it whenever the table exists.
SELECT format('REVOKE INSERT, UPDATE, DELETE ON TABLE %I.alembic_version FROM %I', :'schema', :'runtime')
 WHERE to_regclass(format('%I.alembic_version', :'schema')) IS NOT NULL \gexec

SELECT format('ALTER ROLE %I SET search_path = %I', :'migrator', :'schema') \gexec
SELECT format('ALTER ROLE %I SET search_path = %I', :'runtime', :'schema') \gexec
```

**`10-identity.sql`** (20, 30, 40 are identical apart from the values, and have no audit-log revoke):

```sql
\set schema identity
\set migrator identity_migrator
\set runtime identity_app
\set migrator_env IDENTITY_DB_MIGRATOR_PASSWORD
\set runtime_env IDENTITY_DB_PASSWORD
\ir lib/service_schema.psql
-- C50: the audit log is append-only for the runtime role. The template's re-grant above would restore
-- UPDATE/DELETE on a re-run (e.g. a password rotation), so revoke again whenever the table exists.
SELECT 'REVOKE UPDATE, DELETE ON TABLE identity.audit_log FROM identity_app'
 WHERE to_regclass('identity.audit_log') IS NOT NULL \gexec
```

The identity migration that creates `identity.audit_log` also revokes `UPDATE, DELETE` from `identity_app`
(identity LLD §2.6), so the table is append-only after a fresh bootstrap, after migrations, and after any
bootstrap re-run.

- `20-payroll.sql`: `payroll`, `payroll_migrator`, `payroll_app`, `PAYROLL_DB_MIGRATOR_PASSWORD`, `PAYROLL_DB_PASSWORD`.
- `30-payouts.sql`: `payouts`, `payouts_migrator`, `payouts_app`, `PAYOUTS_DB_MIGRATOR_PASSWORD`, `PAYOUTS_DB_PASSWORD`.
- `40-bank-stub.sql`: `bank_stub`, `bank_stub_migrator`, `bank_stub_app`, `BANK_STUB_DB_MIGRATOR_PASSWORD`, `BANK_STUB_DB_PASSWORD`. Not run in prod (§9.2).

**`90-monitoring.sql`:**

```sql
\getenv exporter_pw POSTGRES_EXPORTER_PASSWORD
\if :{?exporter_pw}
\else
  DO $$ BEGIN RAISE EXCEPTION 'bootstrap: POSTGRES_EXPORTER_PASSWORD is not set'; END $$;
\endif
SELECT (:'exporter_pw' = '') AS exporter_pw_empty \gset
\if :exporter_pw_empty
  DO $$ BEGIN RAISE EXCEPTION 'bootstrap: POSTGRES_EXPORTER_PASSWORD is empty'; END $$;
\endif
SELECT 'CREATE ROLE postgres_exporter LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE CONNECTION LIMIT 3'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres_exporter') \gexec
SELECT format('ALTER ROLE postgres_exporter PASSWORD %L', :'exporter_pw') \gexec
GRANT pg_monitor TO postgres_exporter;                 -- read-only stats views, no table data
GRANT CONNECT ON DATABASE payrun TO postgres_exporter;
ALTER ROLE postgres_exporter SET search_path = pg_catalog;
```

**Migrations** run as the migrator via `pycommon.db.run_alembic_env(target_metadata, schema)` (§3.7):
`version_table_schema = schema`, `include_schemas = True`, filter to that schema only. After
`upgrade`, the helper runs `REVOKE INSERT, UPDATE, DELETE ON <schema>.alembic_version FROM <schema>_app`,
so the runtime role can read the head (for `/readyz`) but never change it. The template re-revokes the
same privileges on every bootstrap run (C63), because its `GRANT … ON ALL TABLES` would restore them.

### 2.2 Redis (`deploy/compose/redis/`; PAY-607, ADR 0007)

**`redis.conf`:**

```conf
port 6379
bind 0.0.0.0
protected-mode yes
aclfile /run/redis/users.acl
dir /data
appendonly yes
appendfsync everysec
aof-use-rdb-preamble yes
maxmemory 256mb
maxmemory-policy noeviction
timeout 0
tcp-keepalive 60
latency-tracking yes
```

256 MB is far above the expected peak: a full stream at `STREAM_MAXLEN` 100000 entries of about
600 bytes is about 60 MB; sessions and limiter keys are a few MB. `RedisMemoryHigh` fires at 80%.

**ACL users** (generated; `#<sha256>` is the hex SHA-256 of the password, so no plaintext is on disk; R12, C3, C27):

| User | Password env | Keys | Commands |
|---|---|---|---|
| `default` | `REDIS_ADMIN_PASSWORD` | `~*` `&*` | `+@all` (admin; used by healthchecks, resilience `FLUSHALL`, operators) |
| `web` | `REDIS_WEB_PASSWORD` | `~web:*` | `+@connection +ping +@read +@write +@keyspace +@transaction -@stream -@scripting -@pubsub -@admin -@dangerous -scan -randomkey` |
| `identity` | `REDIS_IDENTITY_PASSWORD` | `~identity:*` | same as web |
| `payroll` | `REDIS_PAYROLL_PASSWORD` | `~payroll:*`, selector `(~stream:payout.requested +xadd)` | same as web on its keys; `XADD` only on the stream |
| `payout-worker` | `REDIS_PAYOUT_WORKER_PASSWORD` | selectors only | root `+@connection +ping -@scripting -@dangerous`; `(~stream:payout.requested +xreadgroup +xack +xautoclaim +xpending +xinfo +xgroup\|create +xgroup\|delconsumer)`; `(~stream:payout.requested.dlq +xadd +xinfo)` |
| `exporter` | `REDIS_EXPORTER_PASSWORD` | `%R~stream:*` (read-only) | `+@connection +ping -@scripting -@dangerous +info +config\|get +client\|list +slowlog\|get +slowlog\|len +latency\|latest +xinfo +xlen +type +dbsize +memory\|stats` |

Notes:
- **No Lua anywhere (C3):** every user except `default` lacks `@scripting`, so `EVAL`, `EVALSHA`, `FCALL` and `SCRIPT` fail with `NOPERM`. identity's limiter and lockout use `pycommon.ratelimit` (`MULTI` + `INCR` + `EXPIRE NX`) and `SET NX EX` (§3.11).
- **No key enumeration (C63):** key patterns don't filter `SCAN` or `RANDOMKEY` results, so the base rule denies both (`KEYS` is already `@dangerous`). No client uses them.
- `-@stream` for web and identity, plus their key patterns, means `XADD stream:payout.requested` fails with `NOPERM` (the PAY-607 acceptance test).
- **PING** is granted explicitly to every user (R12), for `/readyz` and client health checks; `+@connection` also covers `AUTH`, `HELLO`, `CLIENT SETINFO` (sent by redis-py ≥ 5). `-@dangerous` after it removes `CLIENT KILL` and similar.
- **The worker's commands** match payout-worker LLD §3.3.1: `XREADGROUP`, `XACK`, `XAUTOCLAIM`, `XPENDING`, `XINFO GROUPS` and `XINFO STREAM`, `XGROUP CREATE … MKSTREAM`, `XGROUP DELCONSUMER` (C27; removes stale consumer names), `XADD` to the DLQ (with `MAXLEN ~ 10000`, C21), and `PING`. The `~` key patterns grant read **and** write, which `MKSTREAM` needs to create the stream key (R12). `XINFO` on the DLQ feeds `queue_dlq_messages` and on the main stream `queue_consumer_lag` (Redis 7 `lag`).
- **The exporter** is read-only (`%R~`, R12) and has no `SCAN` or `KEYS`: it is started with `--check-single-streams=stream:payout.requested,stream:payout.requested.dlq`, which needs no key scan.

**`entrypoint.sh`** (POSIX sh; the `redis:7-alpine` image has busybox `sha256sum`):

```sh
#!/bin/sh
set -eu
umask 077
for v in REDIS_ADMIN_PASSWORD REDIS_WEB_PASSWORD REDIS_IDENTITY_PASSWORD REDIS_PAYROLL_PASSWORD \
         REDIS_PAYOUT_WORKER_PASSWORD REDIS_EXPORTER_PASSWORD; do
  eval "val=\${$v:-}"
  [ -n "$val" ] || { echo "redis-acl: missing $v" >&2; exit 1; }
done
h() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }
BASE='+@connection +ping +@read +@write +@keyspace +@transaction -@stream -@scripting -@pubsub -@admin -@dangerous -scan -randomkey'
WORKER_MAIN='~stream:payout.requested +xreadgroup +xack +xautoclaim +xpending +xinfo +xgroup|create +xgroup|delconsumer'
WORKER_DLQ='~stream:payout.requested.dlq +xadd +xinfo'
EXPORTER='+@connection +ping -@scripting -@dangerous +info +config|get +client|list +slowlog|get +slowlog|len +latency|latest +xinfo +xlen +type +dbsize +memory|stats'
acl() {
  cat <<ACL
user default on #$(h "$REDIS_ADMIN_PASSWORD") ~* &* +@all
user web on #$(h "$REDIS_WEB_PASSWORD") resetkeys resetchannels -@all ~web:* $BASE
user identity on #$(h "$REDIS_IDENTITY_PASSWORD") resetkeys resetchannels -@all ~identity:* $BASE
user payroll on #$(h "$REDIS_PAYROLL_PASSWORD") resetkeys resetchannels -@all ~payroll:* $BASE (~stream:payout.requested +xadd)
user payout-worker on #$(h "$REDIS_PAYOUT_WORKER_PASSWORD") resetkeys resetchannels -@all +@connection +ping -@scripting -@dangerous ($WORKER_MAIN) ($WORKER_DLQ)
user exporter on #$(h "$REDIS_EXPORTER_PASSWORD") resetkeys resetchannels -@all %R~stream:* $EXPORTER
ACL
}
if [ "${1:-}" = "--print" ]; then
  acl | sed -e '/^user default /d' -e 's/ #[0-9a-f]\{64\}/ #<sha256>/'   # rule strings for the managed-Redis runbook
  exit 0
fi
mkdir -p /run/redis
acl > /run/redis/users.acl
chown redis:redis /run/redis/users.acl
exec docker-entrypoint.sh redis-server /usr/local/etc/redis/redis.conf
```

**Compose mount:** `redis.conf` and `entrypoint.sh` read-only; `/run/redis` is a `tmpfs` (the
generated file never reaches the `redisdata` volume); `entrypoint: ["/bin/sh", "/usr/local/etc/redis/entrypoint.sh"]`.
The script ends in the official `docker-entrypoint.sh`, which drops to the `redis` user.
In uat and prod the same ACL strings (minus `default`) are applied to the managed Redis's user
management by the operator; `entrypoint.sh --print` prints them (with the hashes masked) for the runbook.
The dev Kubernetes overlay's in-cluster Redis mounts a synced copy of `redis.conf` and `entrypoint.sh` (§9.1).

### 2.3 Service-owned tables (pycommon defines none; C22)

pycommon defines **no** outbox, idempotency-key or processed-events tables, and no table factories.
Each service's own DDL and Alembic migration owns them:

| Table | Owner | Spec |
|---|---|---|
| `payroll.outbox` | payroll | payroll LLD §2.9; relayed with `pycommon.streams.RelayLoop` through payroll's `OutboxRepository` (§3.9) |
| `payroll.idempotency_keys` | payroll | payroll LLD §2.10, §4.8 |
| `payouts.processed_events` (and `payouts.disbursements`) | payout-worker | payout-worker LLD §2.1 |
| `bank_stub.transfers` | bank-stub | bank-stub LLD §2.1 |

Their purges are service maintenance tasks run by `pycommon.maintenance.MaintenanceRunner` (§3.13).

## 3. Interfaces: pycommon

All public names are exported from their module, not re-exported from `pycommon/__init__.py`
(keeps import cost and cycles low). Everything is typed for `mypy --strict`. Time comes from an
injectable `Clock` protocol (`now() -> datetime`, `monotonic() -> float`, `async sleep(s)`), with
`SystemClock` as the default and `FakeClock` in `pycommon.testing`.

### 3.1 `settings.py`

```python
class Env(StrEnum):
    dev = "dev"; uat = "uat"; prod = "prod"

class BaseServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8",
                                      extra="ignore", frozen=True, case_sensitive=False)
    service_name: str                         # set by each subclass as a default: "identity", …
    version: str = Field("0.0.0-dev", alias="APP_VERSION")
    env: Env = Env.dev                        # ENV
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"   # LOG_LEVEL; case-insensitive (C24)
    debug: bool = False                       # DEBUG
    docs_enabled: bool = False                # DOCS_ENABLED: /docs and /openapi.json routes; dev only
    company_id: UUID                          # COMPANY_ID
    # Postgres (C1: the same keys for runtime and migrate processes; no DATABASE_URL)
    db_host: str = "postgres"; db_port: int = 5432; db_name: str = "payrun"
    db_user: str; db_password: SecretStr; db_schema: str    # DB_USER = <schema>_app or <schema>_migrator (C2)
    db_pool_size: int = 10; db_max_overflow: int = 5
    db_statement_timeout_ms: int = 2000       # DB_STATEMENT_TIMEOUT_MS
    db_pool_timeout_s: float = 2.0            # DB_POOL_TIMEOUT_S
    # Redis (C1: no REDIS_URL; unused by bank-stub)
    redis_host: str | None = "redis"; redis_port: int = 6379
    redis_username: str | None = None; redis_password: SecretStr | None = None
    redis_socket_timeout_ms: int = 500        # REDIS_SOCKET_TIMEOUT_MS
    # internal JWT (None for services without JWT-authenticated routes)
    internal_jwt_public_keys: Keyset | None = None   # INTERNAL_JWT_PUBLIC_KEYS, parsed by Keyset.from_json
    jwt_leeway_s: int = 5
    # limits and shared resilience
    max_body_bytes: int = 262_144
    service_rate_limit_user: int = 600        # per minute
    service_rate_limit_worker: int = 6000     # per minute
    breaker_failures: int = 5; breaker_reset_s: float = 30.0
    maintenance_interval_s: int = 60
    # retention windows (PAY-608), read by each service's own purge tasks
    retention_outbox_days: int = 7; retention_processed_events_days: int = 30
    retention_tokens_days: int = 7; retention_bank_details_days: int = 30
    idempotency_ttl_h: int = 24

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, v: object) -> object: ...   # str → v.strip().upper(), then the Literal check (C24)

    def db_url(self, *, user: str | None = None, password: SecretStr | None = None) -> str: ...
        # builds the asyncpg URL from DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD, quoting user and password
    def secret_fields(self) -> dict[str, SecretStr]: ...   # every SecretStr field, by env name

class SettingsError(Exception):
    """Raised by load_settings; message lists field names and error types only."""

def load_settings[S: BaseServiceSettings](cls: type[S]) -> S
def normalise_pem(value: str) -> str      # turns literal "\n" into newlines; strips quotes
```

Behaviour:
- Precedence is process env > `.env` (dev and CI) > defaults (`architecture.md` §7). In containers there is no `.env`.
- `load_settings` catches `ValidationError` and re-raises `SettingsError` built from each error's `loc` and `type` only. Pydantic's `input` (which may be a secret) is dropped. The process then logs `settings.invalid` {fields} and exits 2, the same exit code as a failed startup check (C29).
- **Names (C1, D1):** the fields above are read by their unprefixed env names, in every Python service and in every environment. A service may not rename them (no `<SVC>_DB_HOST`, `<SVC>_REDIS_URL`, `DATABASE_URL` or `REDIS_URL`), and may not add a prefixed duplicate of a base field (such as `PAYROLL_DOCS_ENABLED`).
- Service settings subclass this and add their own keys (owned by the service LLDs), with the service prefix (`IDENTITY_`, `PAYROLL_`, `PAYOUT_`, `BANK_STUB_`) unless the plan or requirements already name the key.
- **`LOG_LEVEL`** accepts any case (`info`, `Info`, `INFO`); the validator upper-cases it before the `Literal` check (C24). Docs and configs write it in upper case.

### 3.2 `logging.py`

```python
REDACT_KEYS: Final[frozenset[str]] = frozenset({
    "password", "token", "authorization", "cookie", "account", "routing", "api_key",   # PAY-NFR-07
    "secret", "private_key", "pem", "set-cookie", "x-api-key", "salary", "amount", "dsn",
})
REDACTED: Final = "[REDACTED]"
request_id_var: ContextVar[str | None]

def redact(value: Any, *, _depth: int = 0) -> Any
def setup_logging(settings: BaseServiceSettings, resource: Resource | None = None) -> None
def get_logger(name: str) -> logging.Logger
def log_event(logger: logging.Logger, level: int, event: str, msg: str = "", **fields: Any) -> None
def user_text(value: str, settings: BaseServiceSettings) -> str | int   # the text in dev, else its length

class JsonFormatter(logging.Formatter): ...
```

Behaviour:
- **`redact`:** recursive over mappings, lists and tuples (depth ≤ 8). A key is masked when its lower-cased name **contains** any deny-listed word (so `bank_account`, `reset_token` and `annual_salary_cents` are masked; the cost is that `account_last4` is masked in logs too, which is harmless). Values are also masked when they look like a JWT (`^eyJ[\w-]+\.[\w-]+\.[\w-]+$`), a `Bearer …` header, or a PEM block, whatever their key. `SecretStr` values render as `REDACTED`.
- **`JsonFormatter`** writes one object per line with `ts`, `level`, `service`, `env`, `version`, `logger`, `event`, `msg`, `trace_id`, `span_id`, `request_id`, then the redacted `fields`, then `exc` (traceback) when present. The message itself is never redacted, so callers never interpolate values into `msg`; a ruff-checked convention plus the test in §6.1 enforce that `log_event` is used.
- **`setup_logging`** installs the formatter on stdout and the OTel `LoggingHandler` (OTLP to Loki) on the root logger, with a `RedactingFilter` in front of the OTel handler so the same `redact` applies to OTLP log attributes. It silences `uvicorn.access` and sets `httpx`/`httpcore` to WARNING (their INFO lines contain URLs).

### 3.3 `otel.py`

```python
os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")   # first statement, before instrumentation imports

@dataclass(frozen=True)
class Telemetry:
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: LoggerProvider
    def shutdown(self, timeout_s: float = 5.0) -> None: ...

def build_resource(settings: BaseServiceSettings) -> Resource          # service.name, service.version, deployment.environment.name
def setup(app: FastAPI | None, settings: BaseServiceSettings, *, engine: AsyncEngine | None = None,
          redis_instrument: bool = True, span_exporter: SpanExporter | None = None,
          metric_reader: MetricReader | None = None) -> Telemetry
def meter(name: str = "pycommon") -> Meter
def tracer(name: str = "pycommon") -> Tracer

SECONDS_BUCKETS: Final = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 15, 30)
SETTLEMENT_BUCKETS: Final = (1, 2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 300, 600, 1800, 3600)
def histogram(name: str, *, unit: str, description: str, buckets: Sequence[float]) -> Histogram

Observation = Mapping[frozenset[tuple[str, str]], float]      # label set → value

def observable_gauge(name: str, *, unit: str, description: str,
                     callback: Callable[[], Observation]) -> ObservableGauge
    # collection-time helper (R26, C18): `callback` runs synchronously on the metric reader's thread;
    # unit "1" raises ValueError (C63); re-registering a name replaces its callback
def clear_gauge_callbacks() -> None                            # pycommon.testing calls it per test

def inject_traceparent() -> str | None                       # W3C traceparent of the current span, None when no valid span
def new_traceparent() -> str                                   # "00-<32 hex>-<16 hex>-00": fresh, valid, not sampled
def context_from_traceparent(tp: str | None) -> Context        # empty Context when tp is None/invalid
```

Behaviour:
- **Exporters:** OTLP gRPC to `OTEL_EXPORTER_OTLP_ENDPOINT` (`http://otel-collector:4317`); metric export every `OTEL_METRIC_EXPORT_INTERVAL` = 15000 ms, cumulative temporality. `OTEL_EXPORTER_OTLP_TIMEOUT` = 3 (**seconds**: the Python gRPC exporter reads seconds, default 10) bounds each export, so the shutdown flush to an unreachable Collector (traces, metrics and logs in turn) stays within the ≤ 10 s close-and-flush budget of a 75 s grace (C67; measured about 5 s by payout-worker `test_shutdown.py`).
- **Sampling (R27, C23):** `OTEL_TRACES_SAMPLER=parentbased_always_on` in every service and every environment, so the apps send 100% of spans; there is no `OTEL_TRACES_SAMPLER_ARG`. `setup` uses `ParentBased(ALWAYS_ON)` when the variable is unset, and logs `otel.sampler.overridden` {sampler} at warning when it is set to anything else. Prod keeps errors, slow traces and a 10% baseline with the Collector's `tail_sampling` (§8.1), not in the SDK.
- **Instrumentation:** FastAPI (`excluded_urls="livez,readyz"`, no header capture, a `server_request_hook` that drops `url.query`), httpx (a request hook that drops `url.query` and never records headers), SQLAlchemy (`engine.sync_engine`; `enable_commenter=False`; `skip_dep_check=True`, because instrumentation 0.66b1 declares sqlalchemy < 2.1 and otherwise instruments nothing, while its engine hooks work on 2.1.6, verified by `test_db_spans.py`; C61), Redis (a `request_hook` that records only the command name, never arguments: arguments can carry session IDs), system metrics (process CPU and memory only).
- **Metric naming rule:** instruments are created with the **final Prometheus name minus `_total`** for counters (the Collector appends `_total`), and with the full name, `_seconds` included, for histograms and gauges with unit `s` (the Collector doesn't duplicate a suffix already present). **Gauges never use unit `"1"` (C63):** the Collector's Prometheus exporter appends `_ratio` to a gauge with that unit, so `circuit_breaker_state` would arrive as `circuit_breaker_state_ratio`. Gauges use a curly-brace annotation (`"{state}"`, `"{entry}"`, `"{run}"`), which adds no suffix, or `"s"` for ages; `observable_gauge` raises `ValueError` for `"1"`. Counters may keep `"1"` (they only get `_total`). `scripts/ci/check_metrics.sh` confirms the names in Prometheus in the e2e job.
- **`observable_gauge`** is D7. It registers an OTel observable gauge whose callback runs at **collection time**, every export interval (15 s), on the `PeriodicExportingMetricReader` thread. So the callback must be synchronous and must not touch the event loop: a service that needs the DB there uses a sync engine with a short statement timeout (payroll LLD §7.2), or reads values its own poller cached (payout-worker LLD §3.3.8). The helper wraps the callback: an exception logs `metrics.gauge_query_failed` {gauge, error_type} at warning and yields **no observation** for that cycle, never a stale or zero value. The SDK keeps only the first instrument per name and ignores later callbacks, so the helper registers one instrument per meter provider and name and **replaces the callback** when the same name is registered again (a second app instance in one process; C63). pycommon registers no domain gauges itself: the queries for `payroll_*` and `outbox_*` (payroll LLD §7.2), `identity_email*` (identity LLD §5.2) and `queue_*` (payout-worker LLD §5.2) are owned by those services (C18).
- **`new_traceparent`** gives producers a valid, unsampled context when no span is active, so `Envelope.traceparent` is always present (C22).
- **`asyncio_event_loop_lag_seconds`** (gauge) is measured by a 1 s ticker task started by `create_app`.

### 3.4 `errors.py`

```python
class AppError(Exception):
    status: ClassVar[int] = 500
    code: ClassVar[str] = "internal_error"
    def __init__(self, message: str | None = None, *, code: str | None = None,
                 details: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None = None,
                 headers: Mapping[str, str] | None = None) -> None: ...

class BadRequest(AppError):        status = 400; code = "bad_request"
class Unauthorized(AppError):      status = 401; code = "invalid_token"
class Forbidden(AppError):         status = 403; code = "forbidden_role"
class NotFound(AppError):          status = 404; code = "not_found"
class Conflict(AppError):          status = 409; code = "conflict"
class PayloadTooLarge(AppError):   status = 413; code = "payload_too_large"
class ValidationFailed(AppError):  status = 422; code = "validation_error"
class RateLimited(AppError):       status = 429; code = "rate_limited"        # sets Retry-After
class ServiceUnavailable(AppError):status = 503; code = "service_unavailable"
class DbTimeout(ServiceUnavailable):     code = "db_timeout"
class DbUnavailable(ServiceUnavailable): code = "db_unavailable"

class ErrorBody(BaseModel):        # {error: {code, message, request_id, details?}}
    error: ErrorDetail

def error_response(status: int, code: str, message: str, *, details: Any = None,
                   headers: Mapping[str, str] | None = None) -> JSONResponse
def install_handlers(app: FastAPI) -> None
```

Handler mapping (PAY-602):

| Exception | Status | `code` | Log |
|---|---|---|---|
| `AppError` subclass | its `status` | its `code` | `http.error` at info (4xx) or warning (503) |
| `RequestValidationError` | 422 | `validation_error`; `details = [{field: "body.email", issue: "value_error"}]`, never the input | info |
| Starlette 404 / 405 | 404 / 405 | `not_found` / `method_not_allowed` | — |
| `asyncpg.QueryCanceledError`, SQLAlchemy pool `TimeoutError`, SQLSTATE `57014` or `55P03` (lock_timeout; C60), or a `TimeoutError` raised inside asyncpg/SQLAlchemy | 503 | `db_timeout` | `db.timeout` warning |
| `asyncpg` / SQLAlchemy connection errors (`OperationalError`, `ConnectionDoesNotExistError`, SQLSTATE `08xxx`/`57Pxx`), or an `OSError` raised inside asyncpg/SQLAlchemy (a refused connect) | 503 | `db_unavailable` | `db.unavailable` warning |
| SQLSTATE `22021` (character_not_in_repertoire, e.g. a NUL byte in text; C63) | 422 | `validation_error`, `details = [{field: "body", issue: "invalid_character"}]` | `http.error` info |
| `redis.exceptions.ConnectionError` / `TimeoutError` not handled by a caller | 503 | `service_unavailable` | `redis.unavailable` warning |
| any other `Exception` | 500 | `internal_error`, message "Something went wrong." | `http.unhandled` error, with the traceback, recorded on the span |

**Only DB-driver errors are DB errors (C63).** `db_error_kind(exc, *, in_db_call=False)` walks the
`__cause__`/`__context__` chain and returns `"timeout"`, `"unavailable"` or `None`. It counts asyncpg and
SQLAlchemy exceptions, Postgres SQLSTATEs, and an `OSError`/`TimeoutError` only when its traceback passes
through asyncpg or SQLAlchemy code (a refused or timed-out connect surfaces raw from `engine.connect()`).
A bare `OSError` or `TimeoutError` from anywhere else (an `asyncio.timeout` in a route, an SMTP socket
error, an httpx connect error) is not a DB error and gives 500 `internal_error`. There is no handler for
bare `OSError`. `map_db_errors()` passes `in_db_call=True`, because its block is a DB call by
construction. `is_invalid_text(exc)` reports SQLSTATE 22021 anywhere in the chain.

Every error body carries `request_id`. No traceback or exception text ever reaches a response.

### 3.5 `auth.py` (PAY-600, PAY-601)

```python
Issuer = Literal["web", "payout-worker"]

@dataclass(frozen=True)
class KeyEntry:
    kid: str
    iss: Issuer
    public_key: Ed25519PublicKey

class Keyset:
    @classmethod
    def from_json(cls, raw: str) -> "Keyset": ...   # {kid: {"iss": "web"|"payout-worker", "pem": "..."}}
    def get(self, kid: str) -> KeyEntry | None: ...
    @property
    def kids(self) -> frozenset[str]: ...

@dataclass(frozen=True)
class Principal:
    iss: Issuer
    sub: str                      # user id (UUID string), "service:web" or "service:payout-worker"
    roles: frozenset[str]
    cid: UUID
    name: str | None              # display name; user tokens only (R9, C10); never logged
    sid: str | None
    jti: str
    exp: datetime
    @property
    def is_service(self) -> bool: ...          # sub starts with "service:"
    @property
    def user_id(self) -> UUID | None: ...
    @property
    def caller_key(self) -> str: ...           # f"{iss}:{sub}", used by rate limits and idempotency

class TokenRejected(Unauthorized):            # 401 invalid_token; .reason is logged, never returned
    reason: Literal["missing", "malformed", "unknown_kid", "bad_alg", "bad_signature", "expired",
                    "not_yet_valid", "wrong_aud", "issuer_mismatch", "issuer_not_allowed", "missing_claim",
                    "lifetime_too_long"]

class TokenVerifier:
    def __init__(self, keyset: Keyset, *, audience: str, leeway_s: int = 5,
                 max_lifetime_s: int = 60, clock: Clock = SystemClock()) -> None: ...
    def verify(self, token: str) -> Principal: ...

class TokenSigner:
    def __init__(self, private_key_pem: str, *, kid: str, issuer: Issuer,
                 clock: Clock = SystemClock()) -> None: ...
    def mint(self, *, aud: str, sub: str, cid: UUID, roles: Iterable[str] = (),
             name: str | None = None, sid: str | None = None, ttl_s: int = 60) -> str: ...
        # claims iss, aud, sub, cid, roles, iat, exp, jti (UUIDv7, C61); name and sid only when given

# FastAPI dependencies
SubjectKind = Literal["user", "service:web", "service:payout-worker"]

def authenticate(request: Request) -> Principal                 # reads Authorization: Bearer; sets request.state.principal
def require(*, issuers: Iterable[Issuer] = ("web",),
            subjects: Iterable[SubjectKind] = ("user",),
            roles: Iterable[str] | None = None, any_role: bool = True) -> Callable[..., Awaitable[Principal]]
```

Verification order (each failure raises `TokenRejected` with the reason; the response is always
401 `invalid_token`, and `auth.token.rejected` is logged with `reason`, `kid`, `iss` and never the token):
1. `Authorization: Bearer <token>` present, else `missing`.
2. Unverified header: `alg == "EdDSA"` (else `bad_alg`); `kid` present and in the keyset (else `unknown_kid`).
3. `jwt.decode(token, entry.public_key, algorithms=["EdDSA"], audience=<service>, leeway=5, options={"require": ["exp", "iat", "iss", "aud", "sub", "jti"]})`; PyJWT errors map to `expired`, `wrong_aud`, `bad_signature`, `not_yet_valid` (iat in the future beyond leeway), `missing_claim`, `malformed`.
4. **Issuer binding:** `claims["iss"] == entry.iss`, else `issuer_mismatch`. This is what stops the worker's key from minting `iss=web` tokens.
5. `exp - iat ≤ max_lifetime_s` (60), else `lifetime_too_long`; `cid` is a UUID, else `missing_claim`; `name`, when present, is a string of 1–200 characters, else `malformed`.

**The 401/403 rule (C30)**, applied by `authenticate` and `require(...)` in this order:

| Condition | Status, `code` | Log |
|---|---|---|
| Missing, malformed, expired or badly signed token; unknown kid; wrong `aud`; `iss` not equal to the kid's bound issuer (`issuer_mismatch`) | 401 `invalid_token` | `auth.token.rejected` {reason, kid, iss} at warning |
| A valid token whose `iss` is not in the route's `issuers` (a web token on `/v1/internal/*`, a worker token on a user route; PAY-411) | 401 `invalid_token` | `auth.token.rejected` {reason: `issuer_not_allowed`, iss} |
| A valid token of the wrong kind for the route: its subject kind (`user` when `sub` is a UUID, else the literal `service:web` or `service:payout-worker`) is not in `subjects` (for example `service:web` on a user route, or a user token on identity's public-flow routes) | 403 `forbidden_role` | `auth.forbidden` {route, subject_kind} at info |
| A valid token of the right kind with a missing role (`any_role=True`: none of `roles`; `False`: not all of them) | 403 `forbidden_role` | `auth.forbidden` {route, missing_roles} at info |

`create_app` attaches `authenticate` to every router except health; handlers add `require(...)` per route.
payroll's internal router uses `require(issuers=("payout-worker",), subjects=("service:payout-worker",))`;
identity's four public-flow routes use `require(subjects=("service:web",))`. The token, `name` and
`sid` are never logged.

`Keyset.from_json` rejects (as `SettingsError`): non-object JSON, an `iss` outside `Issuer`, a PEM
that isn't an Ed25519 public key, and duplicate kids. Rotation follows `auth.md`: add the new kid
everywhere, switch the signer, remove the old kid after 60 s.

### 3.6 `health.py` (PAY-605)

```python
class HealthCheck(Protocol):
    name: str
    async def __call__(self) -> None: ...      # raises on failure

def db_check(engine: AsyncEngine) -> HealthCheck                         # SELECT 1
def redis_check(redis: Redis) -> HealthCheck                             # PING
def migrations_check(engine: AsyncEngine, *, schema: str, script_location: str) -> HealthCheck
def task_check(name: str, is_alive: Callable[[], bool]) -> HealthCheck   # e.g. the worker's consumer loop
def health_router(checks: Sequence[HealthCheck], *, timeout_s: float = 1.0) -> APIRouter
```

- `GET /livez` → 200 `{"status": "ok"}`; no I/O.
- `GET /readyz` runs every check concurrently, each under `asyncio.timeout(timeout_s)`; 200 `{"status": "ok", "checks": {"db": "ok", "redis": "ok", "migrations": "ok"}}`, or 503 with `"fail"` for the failing ones. No error text in the body; failures log `health.check_failed` {check, error_type} at warning, at most once per 30 s per check.
- `migrations_check` reads the Alembic head(s) from the service's script directory once at start-up, then compares with `SELECT version_num FROM <schema>.alembic_version`.
- Health routes have no auth, no rate limit, and are excluded from tracing and `http.request` logs.

### 3.7 `db.py`

```python
def create_engine(settings: BaseServiceSettings, *, user: str | None = None,
                  password: SecretStr | None = None) -> AsyncEngine
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]
@asynccontextmanager
async def transaction(sf: async_sessionmaker[AsyncSession], *, budget_s: float | None = None) -> AsyncIterator[AsyncSession]
@contextmanager
def map_db_errors(operation: str = "db", *, budget_s: float | None = None) -> Iterator[None]   # raises DbTimeout / DbUnavailable / ValidationFailed (22021)
def db_budget_s(settings: BaseServiceSettings) -> float   # DB_STATEMENT_TIMEOUT_MS / 1000 + DB_POOL_TIMEOUT_S; create_engine records it as the default
def new_id() -> UUID                                  # uuid.uuid7() (Python 3.14 stdlib)
def advisory_lock_key(name: str) -> int               # signed 64-bit from sha256(name)[:8]
class Base(DeclarativeBase): ...
class TimestampMixin:                                  # created_at, updated_at timestamptz, server defaults, onupdate
    created_at: Mapped[datetime]; updated_at: Mapped[datetime]
def run_alembic_env(target_metadata: MetaData, *, schema: str, runtime_role: str) -> None
```

`map_db_errors(operation)` logs `db.timeout` / `db.unavailable` {operation} and raises `DbTimeout` /
`DbUnavailable`; text Postgres rejects (SQLSTATE 22021) raises `ValidationFailed` (422, C63). Services
that reject NUL in their request models never reach that path; it's the defence in depth.

`create_engine` uses `postgresql+asyncpg`, `pool_size`, `max_overflow`, `pool_timeout = DB_POOL_TIMEOUT_S`,
`pool_pre_ping = True`, `pool_recycle = 1800`, and `connect_args = {"server_settings": {"statement_timeout":
str(DB_STATEMENT_TIMEOUT_MS), "lock_timeout": "1000", "idle_in_transaction_session_timeout": "10000",
"application_name": service_name, "search_path": db_schema}, "timeout": 2, "command_timeout":
DB_STATEMENT_TIMEOUT_MS / 1000 + 0.5}`. Timeouts come from three layers, all mapped to 503 `db_timeout`:
a slow *query* is cancelled by Postgres at `DB_STATEMENT_TIMEOUT_MS` (`QueryCanceledError`); a slow
*link* (network latency, a stalled server) isn't visible to `statement_timeout`, which bounds only
server-side execution, so asyncpg's per-command `command_timeout` bounds each command; and the whole
unit of DB work in `transaction()` / `map_db_errors()` has a budget of `DB_STATEMENT_TIMEOUT_MS / 1000 +
DB_POOL_TIMEOUT_S` (4 s), cleanup included. The budget behaves like `asyncio.timeout` but keeps
cancelling until the block exits, so the rollback and connection release can't wait on the slow link
again; SQLAlchemy terminates a connection whose release is cancelled. Without the budget, ~10 slow
round trips (pre-ping, prepare, execute, commit) took ~20 s at 2 s latency and then succeeded (S8
finding, `tests/resilience/test_slow_db.py`, pycommon `test_db_slow_link.py`). A dead DB yields a fast connect failure,
mapped to 503 `db_unavailable`, and `pool_pre_ping` discards broken connections after recovery
(the "no stuck pool connections" row of plan §12.1). `run_alembic_env` is called from each
service's `migrations/env.py`; it reads the migrator credentials from `DB_USER` / `DB_PASSWORD`.

### 3.8 `redis_client.py`

```python
def create_redis(settings: BaseServiceSettings, *, socket_timeout_ms: int | None = None,
                 client_name: str | None = None) -> Redis        # redis.asyncio, decode_responses=False
```

Connects as `REDIS_USERNAME` / `REDIS_PASSWORD`, `socket_connect_timeout` = `socket_timeout` =
`REDIS_SOCKET_TIMEOUT_MS` (500 ms), `health_check_interval = 15`, `retry_on_timeout = False` (callers
decide). **Blocking reads need a second client:** `XREADGROUP … BLOCK PAYOUT_XREAD_BLOCK_MS` (5000 ms) would
always hit a 500 ms socket timeout, so the worker's own consumer (payout-worker LLD §3.3.1; C21) creates a
second client with `create_redis(settings, socket_timeout_ms=PAYOUT_XREAD_BLOCK_MS + REDIS_SOCKET_TIMEOUT_MS)`
(5.5 s) and uses it only for the blocking read.

### 3.9 `streams.py`

pycommon gives producers the envelope, the stream write and a relay loop. It defines **no tables**
and **no consumer** (C22, C21): payroll's DDL owns the outbox and idempotency tables, and the
payout-worker owns its consumer, its processed-events table and the DLQ write (payout-worker LLD §3.2–§3.3).

```python
class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: UUID
    type: str
    version: int
    occurred_at: datetime              # UTC, serialised RFC 3339 with "Z" and milliseconds
    producer: str
    traceparent: str                   # required (C22); W3C format, validated by regex
    data: dict[str, Any]
    @classmethod
    def new(cls, *, type: str, version: int, producer: str, data: Mapping[str, Any],
            clock: Clock = SystemClock()) -> "Envelope": ...
        # event_id = new_id(); occurred_at = now; traceparent = inject_traceparent() or new_traceparent()
    def to_json(self) -> str: ...      # compact JSON, the stream's `envelope` field

class StreamProducer:
    def __init__(self, redis: Redis) -> None: ...
    async def publish(self, stream: str, *, event_id: UUID | str, type: str, envelope_json: str,
                      maxlen: int) -> bytes: ...
        # XADD <stream> MAXLEN ~ <maxlen> * event_id <id> type <type> envelope <json>   (R7)
    async def publish_many(self, stream: str, items: Sequence[StreamItem], *,
                           maxlen: int) -> list[bytes | Exception]: ...
        # one non-transactional pipeline, raise_on_error=False: per-item entry id or the error

@dataclass(frozen=True)
class StreamItem:
    event_id: UUID
    type: str
    envelope_json: str                 # already carries the traceparent the consumer should continue
    ref: Any = None                    # owner's opaque handle (e.g. payroll: (payout_id, attempt, span)); the loop passes it back untouched

# The owner opens any per-item PRODUCER span inside claim_unsent (when it builds envelope_json) and
# closes it in mark_sent / mark_failed; RelayLoop publishes the whole batch with one publish_many pipeline.
class OutboxRepository(Protocol):      # implemented by the owning service over its own table
    async def claim_unsent(self, session: AsyncSession, *, limit: int) -> Sequence[StreamItem]: ...
        # SELECT … WHERE sent_at IS NULL ORDER BY created_at, id LIMIT :limit FOR UPDATE SKIP LOCKED
    async def mark_sent(self, session: AsyncSession, items: Sequence[StreamItem]) -> None: ...
    async def mark_failed(self, session: AsyncSession, items: Sequence[StreamItem], *, error_type: str) -> None: ...

class RelayLoop:
    def __init__(self, sf: async_sessionmaker[AsyncSession], producer: StreamProducer,
                 repo: OutboxRepository, *, stream: str, maxlen: int, batch_size: int = 100,
                 poll_interval_s: float = 0.2, max_backoff_s: float = 5.0,
                 clock: Clock = SystemClock()) -> None: ...
    def wake(self) -> None: ...                 # called from the owner's after-commit hook
    async def relay_once(self) -> int: ...      # rows sent
    async def run(self, stop: asyncio.Event) -> None: ...
```

**Stream entry fields (R7):** `event_id` (string), `type` (`payout.requested`), `envelope` (the
envelope JSON). The payout-worker reads exactly these three. `publish` and `publish_many` record a span
`"<stream> publish"` (kind PRODUCER, `messaging.system=redis`, `messaging.destination.name`,
`messaging.message.id = event_id`); the owner may instead start its own publish span and put that
span's context in `envelope_json`, as payroll does (payroll LLD §5.2).

**Relay behaviour** (PAY-402):
1. Wait for `wake()` or `poll_interval_s` (200 ms), whichever comes first.
2. In one transaction: `repo.claim_unsent(limit=batch_size)`; nothing claimed → commit and loop.
3. `publish_many` the claimed items in order.
4. `repo.mark_sent` for the items that got an entry id, `repo.mark_failed(error_type=<class name>)` for the rest; commit.
5. Any failure (per item, or the whole pipeline raising): log `outbox.relay.error` {error_type, batch_size, failed, backoff_ms} at error, then sleep full-jitter `min(0.2 × 2^k, max_backoff_s)` s, `k += 1`; a clean batch resets `k`. A row that was XADDed but whose commit failed is sent again later: delivery is at least once, and the consumer is idempotent.
6. A batch with at least one sent item logs `outbox.relay.published` {count, duration_ms} at info (C63).
7. A full batch loops at once; on `stop`, the current batch finishes, then `run` returns.

Several replicas are safe because the repository claims with `SKIP LOCKED`. The `outbox_unsent`
and `outbox_oldest_age_seconds` gauges are payroll's (payroll LLD §7.2, C18), not the loop's.

**DLQ** (written by the worker, not pycommon; C21): `XADD stream:payout.requested.dlq MAXLEN ~ 10000 *`
with the fields `envelope, source_id, error_kind, error_code, error, delivery_count, consumer, failed_at`,
specified in `contracts/events/payout.requested.dlq.v1.json`. `queue_messages_total{stream, group, outcome}`
takes `ok`, `retry` and `dlq` and is pre-initialised with `add(0)` for each at start (C21).

### 3.10 `http.py` (resilient client; plan §9, `external-apis.md`)

```python
@dataclass(frozen=True)
class ProviderPolicy:
    name: str                          # metric/breaker label: "bank", "payroll", "smtp"
    base_url: str = ""
    connect_timeout: float = 1.0
    read_timeout: float = 5.0
    total_deadline: float = 15.0       # retries included
    max_retries: int = 3
    retry_on: frozenset[int] = frozenset({429, 502, 503, 504})
    retry_on_timeout: bool = True
    backoff_base_s: float = 0.2
    backoff_cap_s: float = 2.0
    breaker_failures: int = 5          # BREAKER_FAILURES
    breaker_reset_s: float = 30.0      # BREAKER_RESET_S

class BreakerState(IntEnum):
    closed = 0; half_open = 1; open = 2

class CircuitBreaker:
    def __init__(self, target: str, *, failures: int, reset_s: float, clock: Clock = SystemClock()) -> None: ...
    @property
    def state(self) -> BreakerState: ...
    def allow(self) -> bool: ...        # False while open; in half-open, True for exactly one trial
    def record_success(self) -> None: ...
    def record_failure(self) -> None: ...
    # Construction registers the `circuit_breaker_state{target}` observable gauge, which reports the
    # current state (0 closed at start), so the series exists on a healthy stack (check_metrics.sh).

class ProviderError(Exception):
    provider: str; operation: str
class ProviderTimeout(ProviderError): ...                 # connect/read timeout or the total deadline
class ProviderRateLimited(ProviderError):  retry_after_s: float | None
class ProviderUnavailable(ProviderError):  reason: Literal["breaker_open", "connect_error", "server_error"]
class ProviderRejected(ProviderError):     status: int    # 4xx other than 429

class Guard:
    """Deadline + retries + breaker + metrics for any async call (used directly for SMTP)."""
    def __init__(self, policy: ProviderPolicy, *, clock: Clock = SystemClock()) -> None: ...
    async def call[T](self, operation: str, fn: Callable[[], Awaitable[T]], *,
                      retryable: Callable[[BaseException], bool], deadline_s: float | None = None) -> T: ...

class ResilientClient:
    def __init__(self, policy: ProviderPolicy, *,
                 auth_headers: Callable[[], Awaitable[Mapping[str, str]]] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, clock: Clock = SystemClock()) -> None: ...
    async def request(self, method: str, path: str, *, operation: str, json: Any = None,
                      params: Mapping[str, str] | None = None, headers: Mapping[str, str] | None = None,
                      idempotency_key: str | None = None, deadline_s: float | None = None) -> httpx.Response: ...
    async def request_raw(self, method: str, path: str, *, operation: str, json: Any = None,
                          params: Mapping[str, str] | None = None, headers: Mapping[str, str] | None = None,
                          idempotency_key: str | None = None, deadline_s: float | None = None) -> httpx.Response: ...
    async def aclose(self) -> None: ...
```

Behaviour (each line has a unit test with respx and `FakeClock`):
- **Deadline:** `asyncio.timeout(deadline_s or total_deadline)` around the whole call; an expired deadline raises `ProviderTimeout`. Per attempt `httpx.Timeout(connect=…, read=…, write=read, pool=connect)`.
- **Retries** only when the request is idempotent (GET, HEAD, PUT, DELETE) **or** carries `idempotency_key`, which is sent as `Idempotency-Key` and is the same on every attempt. Retries on `retry_on` statuses and, when `retry_on_timeout`, on timeouts and connect errors. Never on other 4xx. Backoff is full jitter `uniform(0, min(cap, base × 2^n))`; `Retry-After` (seconds or HTTP date) is honoured when it fits in the remaining deadline, otherwise the call stops and raises.
- **`auth_headers`** is awaited per attempt, so a 60 s JWT is re-minted on long retry chains.
- **Breaker:** checked before each attempt; open → `ProviderUnavailable("breaker_open")` without sending. Failures counted: timeouts, connect errors, 5xx (429 and 4xx are not breaker failures: they prove the target is up). Transitions are logged (`external.breaker.opened` at warning, `external.breaker.half_open`, `external.breaker.closed` at info) and exported as `circuit_breaker_state{target}`.
- **`request`** maps the final response: 2xx returned; 429 → `ProviderRateLimited`; 5xx → `ProviderUnavailable("server_error")`; other 4xx → `ProviderRejected(status)`.
- **`request_raw`** returns any final HTTP response unmapped (after the same retries), and raises only for timeouts, connect errors and an open breaker. The worker's bank client uses it, because ADR 0003 (e) overrides the generic `ProviderRejected` mapping: only a 2xx `rejected` body is definite.
- **Observability:** span per attempt `"<provider> <operation>"` (kind CLIENT) with `provider`, `operation`, `attempt`, `http.response.status_code`; `external_requests_total{provider, operation, outcome}` per attempt, with `outcome` ∈ `ok` (2xx), `client_error` (4xx except 429), `rate_limited`, `server_error`, `timeout`, `connect_error`, `breaker_open`; `external_request_duration_seconds{provider, operation}` (`SECONDS_BUCKETS`); log `external.request.retry` (warning: provider, operation, attempt, reason, delay_ms) and `external.request.failed`. Bodies, headers and query strings are never logged or put on spans.

Instances: worker → bank (`name="bank"`, connect 1 s, read 5 s, 3 retries, deadline `BANK_TOTAL_DEADLINE_S`); worker → payroll (`name="payroll"`, connect 1 s, read 2 s, 3 retries, deadline `PAYOUT_PAYROLL_CALL_DEADLINE_S`); identity → SMTP (`Guard`, `name="smtp"`, 5 s). So `external_requests_total` has `provider` ∈ `bank`, `payroll`, `smtp`.

### 3.11 `ratelimit.py` (PAY-603)

```python
class FailMode(StrEnum):
    open = "open"; closed = "closed"

@dataclass(frozen=True)
class Limit:
    scope: str             # metric label and key part: "caller", "login_account", "login_ip", …
    limit: int
    window_s: int
    fail_mode: FailMode

@dataclass(frozen=True)
class Decision:
    allowed: bool
    remaining: int
    retry_after_s: int
    degraded: bool         # True when Redis failed and fail_mode was open

class RateLimiter:
    def __init__(self, redis: Redis, *, prefix: str, component: str, clock: Clock = SystemClock()) -> None: ...
        # prefix "identity:rl" / "payroll:rl"; component is the rate_limiter_errors_total label
        # __init__ calls rate_limiter_errors_total.add(0, {component}) so the series exists before any error
    async def hit(self, limit: Limit, subject: str) -> Decision: ...
    async def check(self, limit: Limit, subject: str) -> None: ...   # raises RateLimited / ServiceUnavailable
    @property
    def component(self) -> str: ...
    def record_denied(self, scope: str) -> None: ...                  # rate_limited_total{scope} +1 (C63)
    def record_error(self, exc: BaseException | None = None, *,
                     fail_mode: FailMode | str = FailMode.open) -> None: ...
        # rate_limiter_errors_total{component} +1 and ratelimit.backend_error (≤ 1 per 10 s); the caller
        # then allows or refuses (C63)

def caller_rate_limit(settings: BaseServiceSettings, *,
                      exempt_subs: Iterable[str] = ("service:web",)) -> Callable[..., Awaitable[None]]
    # FastAPI dependency (C17)
```

- **Algorithm (D6):** fixed window. Key `"{prefix}:{scope}:{sha256(subject)[:32]}:{now // window_s}"`; in one `MULTI`: `INCR key`, `EXPIRE key window_s+1 NX`. Allowed when the count ≤ `limit`; otherwise `retry_after_s = window_end − now` (≥ 1). The subject is hashed, so emails and IPs never appear in Redis keys. Bursts up to 2× at a window edge are accepted at this scale.
- **Redis error or timeout** (500 ms socket timeout): `rate_limiter_errors_total{component}` +1 and log `ratelimit.backend_error` (warning, at most once per 10 s); then `open` → `Decision(allowed=True, degraded=True)`; `closed` → `ServiceUnavailable` (503 `service_unavailable`).
- **Denied:** `rate_limited_total{scope}` +1; `check` raises `RateLimited` with `Retry-After`.
- **Service decisions (C63):** a service that denies or fails on its own Redis call (identity's lockout `SET NX EX` and its "locked" check) reports through the same limiter: `record_denied(scope)` and `record_error(exc, fail_mode=…)`. Services create no `rate_limited` or `rate_limiter_errors` counters of their own.
- **`caller_rate_limit` (PAY-603, C17):** scope `caller`, component `caller`, subject `Principal.caller_key` (`"<iss>:<sub>"`), so the key is `"{prefix}:caller:{sha256(caller_key)[:32]}:{window}"` in every service; identity and payroll use this format and build no caller keys of their own. A principal whose `sub` is in `exempt_subs` (default `("service:web",)`: the BFF's public flows, which identity limits per IP instead) is not counted. `iss=payout-worker` uses `SERVICE_RATE_LIMIT_WORKER` (6000/min), every other caller `SERVICE_RATE_LIMIT_USER` (600/min), window 60 s; fail open. Login, reset and invite-accept limits (fail closed) are built by identity with the same `RateLimiter`.

### 3.12 `startup_checks.py` (PAY-605)

```python
@dataclass(frozen=True)
class CheckFailure:
    setting: str          # env var name, e.g. "PAYROLL_FIELD_ACTIVE_KID"
    reason: str           # "missing", "dev_default", "dev_kid", "debug_on", "docs_on", "bank_stub_url", "timing_invariant",
                          # "not_deployable" (bank-stub refuses ENV=prod)

StartupCheck = Callable[[BaseServiceSettings], Iterable[CheckFailure]]

DEV_DEFAULTS: Final = frozenset({"", "changeme", "change-me", "password", "secret", "dev", "test", "REPLACE_ME", "placeholder"})
DEV_KID_PREFIXES: Final = ("dev-", "test-")

def check_secrets(s: BaseServiceSettings) -> Iterable[CheckFailure]          # every SecretStr: present, not in DEV_DEFAULTS, length ≥ 16;
    # optional service secrets that default to None (SMTP_PASSWORD) are skipped, and services enforce
    # their own "required in prod" rules as extra checks (C59)
def check_jwt_kids(s: BaseServiceSettings) -> Iterable[CheckFailure]         # INTERNAL_JWT_PUBLIC_KEYS kids and any *_KID field
def check_debug_and_docs(s: BaseServiceSettings) -> Iterable[CheckFailure]
def check_field_keys(keys_json: str, active_kid: str, *, setting: str = "PAYROLL_FIELD_KEYS") -> Iterable[CheckFailure]
def check_bank_base_url(url: str, *, env: Env) -> Iterable[CheckFailure]      # prod: host not bank-stub, not localhost, not *.invalid, no "placeholder"/"example"
def check_claim_idle(claim_min_idle_ms: int, bank_total_deadline_s: float,
                     payroll_call_deadline_s: float) -> Iterable[CheckFailure]
    # claim_min_idle_ms/1000 > bank_total_deadline_s + 2*payroll_call_deadline_s + 10
def check_republish_after(republish_after_s: float, max_deliveries: int, claim_min_idle_ms: int,
                          bank_total_deadline_s: float) -> Iterable[CheckFailure]
    # republish_after_s > max_deliveries * claim_min_idle_ms/1000 + bank_total_deadline_s

def check_mark_rejected_wait(mark_rejected_min_wait_s: float, bank_total_deadline_s: float,
                             payroll_call_deadline_s: float) -> Iterable[CheckFailure]
    # mark_rejected_min_wait_s > (bank_total_deadline_s + 2*payroll_call_deadline_s + 10) + 30   (C48)

def run_startup_checks(settings: BaseServiceSettings, extra: Sequence[StartupCheck] = (), *,
                       fatal: Sequence[StartupCheck] = ()) -> None
```

- `run_startup_checks` runs the common checks plus `extra`. In **uat and prod** any failure logs `startup.check_failed` {setting, reason} at error for **each** failure (never the value), then exits with code 2. In **dev** the same failures are logged at warning and start-up continues, so a developer sees them. `fatal` checks log at error and exit 2 in **every** env, dev included: `create_app` passes the JWT keyset check there, because without `INTERNAL_JWT_PUBLIC_KEYS` no route could authenticate (C63).
- The timing checks are extras: payroll registers `check_republish_after` and `check_mark_rejected_wait`; the worker registers `check_claim_idle` and `check_republish_after`. Both services therefore read the shared timing keys (`PAYOUT_MAX_DELIVERIES`, `PAYOUT_CLAIM_MIN_IDLE_MS`, `BANK_TOTAL_DEADLINE_S`, `PAYOUT_PAYROLL_CALL_DEADLINE_S`, `PAYOUT_REPUBLISH_AFTER_S`, `PAYOUT_MARK_REJECTED_MIN_WAIT_S`) from one shared source: `x-payout-timing` in Compose, the `payout-timing` ConfigMap in Kubernetes.
- **The defaults pass all three checks:** republish 360 > 5 × 60 + 15 = 315 (R1; the old plan value 300 failed); claim idle 60 > 15 + 2 × 5 + 10 = 35; mark_rejected wait 120 > 35 + 30 = 65 (C48).
- **Every Python service uses this module (C29):** a failed check logs `startup.check_failed` {setting, reason} and exits **2**; services add their checks as `extra` and log no other start-up refusal event (no `worker.startup.refused`, `bank_stub.startup.refused` or exit 1). The web does the same in TypeScript: `config.invalid`, exit 2.

### 3.13 `maintenance.py` (PAY-608)

```python
@dataclass(frozen=True)
class MaintenanceTask:
    name: str                                                 # "purge_outbox", "republish", …
    fn: Callable[[AsyncSession], Awaitable[int]]              # returns rows affected; owns no commit
    # Tasks that need more than the session (settings, clock, Redis) bind it with a closure or
    # functools.partial when the service builds its task list (e.g. payroll republish, worker prune_consumers).

class MaintenanceRunner:
    def __init__(self, engine: AsyncEngine, sf: async_sessionmaker[AsyncSession], *, lock_name: str,
                 tasks: Sequence[MaintenanceTask], interval_s: float = 60, jitter: float = 0.1,
                 clock: Clock = SystemClock()) -> None: ...
    async def tick(self) -> bool: ...          # True when this replica held the lock and ran
    async def run(self, stop: asyncio.Event) -> None: ...

```

- **Lock:** on a dedicated pooled connection, `SELECT pg_try_advisory_lock(advisory_lock_key(lock_name))` (`lock_name = "<service>:maintenance"`). Not acquired → skip this tick (another replica is running), log `maintenance.skipped` at debug. Acquired → run each task **in its own transaction** (one session each, committed on success, rolled back on error), then `pg_advisory_unlock` in a `finally`. If the connection dies, Postgres releases the lock. The runtime role needs no extra privilege for advisory locks.
- **Log events (C44):** every service uses these names. A completed task logs `maintenance.task.completed` {task, affected, duration_ms} at info; a failing task logs `maintenance.task.failed` {task, error_type} at error and doesn't stop the others; a tick that didn't get the lock logs `maintenance.skipped` at debug. Metrics `maintenance_runs_total{task, outcome}` and `maintenance_duration_seconds{task}`.
- Ticks every `MAINTENANCE_INTERVAL_S` ± 10% jitter; the first tick is 5 s after start-up.
- **Tasks are the services'** (C22): pycommon owns no tables, so it ships no purge tasks. payroll supplies outbox (`RETENTION_OUTBOX_DAYS`), idempotency-key (`IDEMPOTENCY_TTL_H`), bank-detail (`RETENTION_BANK_DETAILS_DAYS`) and re-publish tasks; the worker the processed-events purge (`RETENTION_PROCESSED_EVENTS_DAYS`); identity the token, email and audit purges. Each task deletes in batches of 1000 and returns the row count.

### 3.14 `app.py`

```python
@dataclass
class AppContext:
    settings: BaseServiceSettings
    engine: AsyncEngine | None
    sessions: async_sessionmaker[AsyncSession] | None
    redis: Redis | None
    verifier: TokenVerifier | None
    limiter: RateLimiter | None
    telemetry: Telemetry | None

BackgroundTask = Callable[[AppContext, asyncio.Event], Awaitable[None]]

def create_app(settings: BaseServiceSettings, *,
               routers: Sequence[APIRouter] = (),
               public_routers: Sequence[APIRouter] = (),          # no internal JWT (bank-stub uses its API key dependency)
               use_db: bool = True, use_redis: bool = True, use_jwt: bool = True,
               migrations_script_location: str | None = None,
               extra_health_checks: Callable[[AppContext], Sequence[HealthCheck]] | None = None,
               startup_checks: Sequence[StartupCheck] = (),
               background: Sequence[BackgroundTask] = (),
               on_startup: Callable[[AppContext], Awaitable[None]] | None = None,
               shutdown_timeout_s: float = 60.0,
               instrument: bool = True) -> FastAPI
def get_ctx(request: Request) -> AppContext                    # FastAPI dependency
def build_openapi(build_app: Callable[[BaseServiceSettings], FastAPI], settings: BaseServiceSettings) -> dict[str, Any]
```

Order of assembly (outermost first):
1. `FastAPI(title=service_name, version=version, docs_url="/docs" if docs_enabled else None, redoc_url=None, openapi_url="/openapi.json" if docs_enabled else None)`. `app.openapi()` still works for `export_openapi.py`.
2. OTel instrumentation (`otel.setup`), unless `instrument=False` (OpenAPI export and unit tests).
3. Request-context middleware: `request_id` from `X-Request-ID` if it matches `^[A-Za-z0-9-]{1,64}$`, else a new UUID4; set `request_id_var`; add `x-request-id` and `x-trace-id` response headers; log one `http.request` {method, route (template), status, duration_ms, caller (sha256 of `caller_key`, first 16 hex)} at info (warning for 5xx).
4. Body-size limit (pure ASGI): `Content-Length` > `MAX_BODY_BYTES` → 413 at once; otherwise counts streamed bytes and aborts with 413 `payload_too_large` past the limit.
5. Error handlers (`install_handlers`).
6. Routers: `routers` included with `dependencies=[Depends(authenticate)]` (and `Depends(caller_rate_limit(settings))`, exempting `service:web`, when Redis is used); `public_routers` as given; `health_router` with no dependencies.
7. Lifespan:
   1. `setup_logging`; `run_startup_checks(settings, startup_checks, fatal=[jwt keyset present])` (`startup.check_failed`, exit 2 in uat/prod on failure, C29; a missing keyset exits 2 in every env, C63), **before** any connection is opened;
   2. create the engine, Redis client, verifier and limiter as configured; build `AppContext` on `app.state.ctx`;
   3. `on_startup(ctx)`; log `service.started` {version, env};
   4. start each `background` task with a shared `stop` event (relay, maintenance, gauges, the worker's consumer, the event-loop-lag ticker);
   5. on shutdown: set `stop`, wait for tasks up to `shutdown_timeout_s`, cancel stragglers, close Redis, dispose the engine, `telemetry.shutdown()`; log `service.stopped`.
   A background task that crashes logs `background.task.crashed` {task} at error and is restarted after 1 s (doubling to 30 s), so one bad tick never kills the relay or the consumer.

**`serve.py`:** `python -m pycommon.serve` reads `PYCOMMON_APP` (`identity.main:app`), host `0.0.0.0`, port `PORT` (8000), and runs `uvicorn.Server(Config(app, access_log=False, proxy_headers=False, server_header=False, timeout_graceful_shutdown=<from settings>, lifespan="on"))`. The worker is served the same way: its `app` has only health routes and the consumer as a background task.

**`testing.py`** (pytest plugin `pycommon.testing`, registered with `pytest_plugins`): fixtures `pg_bootstrapped` (Postgres 16 with `deploy/compose/init` mounted at `/docker-entrypoint-initdb.d` and generated test passwords in the container env), `db_env_for(role)` (the C1 keys `DB_HOST` … `DB_SCHEMA` for that role), `alembic_upgrade(service)`, `redis_acl` (Redis 7 with `deploy/compose/redis/` mounted and the same entrypoint), `redis_env_for(user)` (the C1 keys `REDIS_HOST`, `REDIS_PORT`, `REDIS_USERNAME`, `REDIS_PASSWORD` for that ACL user), `token_factory` (a throwaway Ed25519 key pair, `TokenSigner` and a matching `Keyset`), `otel_memory` (in-memory span exporter, `InMemoryMetricReader` and log exporter), `fake_clock`, `caplog_json`.
**OTel isolation (C61, C63):** the plugin's `pytest_configure` installs **one** in-memory provider set as the
OTel globals for the whole process (`session_otel()`), and nothing resets the globals afterwards, so
instruments created at import time or by an earlier test keep reporting to the same provider and suites can
share a process. `otel_memory` clears the span and log exporters and the pending metric data per test, and
drops every gauge callback (`otel.clear_gauge_callbacks()`). Synchronous counters and histograms use delta
temporality in this reader, and `OtelMemory.metric_points` adds the deltas up, so each test sees counts from
its own start; gauges show what the current collection reports. Service tests reuse them, so bootstrap SQL and ACLs are exercised in every service's integration suite (PAY-606, 607).

### 3.15 `scripts/`

#### `scripts/dev-env.sh`

`scripts/dev-env.sh [--env-file PATH] [--k8s-dev [--k8s-dev-file PATH]]`. Bash, `set -euo pipefail`, shellcheck-clean. It runs from the repository root wherever it's called from; `--env-file` and `--k8s-dev-file` paths are taken relative to the caller's directory.

- **Idempotent:** it reads the existing `.env` (default `./.env`, mode 0600) and appends only missing keys. Existing values are kept, except the *derived* keys (ports and keysets), which are recomputed every run from their sources. It never prints a secret; it prints the names of keys it added.
- **Tooling:** needs OpenSSL ≥ 3 for Ed25519 (`openssl genpkey -algorithm ed25519`). macOS's `/usr/bin/openssl` is LibreSSL, so the script prefers `$(brew --prefix openssl@3)/bin/openssl` and fails with a clear message when no OpenSSL 3 is found. CI runners have OpenSSL 3.
- **Encoding in `.env`:** PEMs are written double-quoted with `\n` escapes (Compose expands `\n` in double-quoted values); JSON values are single-quoted (taken literally, so the JSON's own `\n` escapes reach the parser intact). `pycommon.settings.normalise_pem` and the web's loader accept both forms, so `source .env` in bash also works.
- `--k8s-dev` also writes `deploy/k8s/overlays/dev/secrets.env` (git-ignored; `--k8s-dev-file` overrides the path, for tests) from the same values, for the dev overlay's `secretGenerator`. Kustomize reads env files literally, so each value is written **unquoted** on one line: JSON as-is, PEMs with literal `\n` escapes. The Compose-only keys (`APP_PORT_BASE`, `COMPOSE_PROJECT_NAME` and the host ports) are left out; `SMTP_PORT` stays. The file is mode 0600.

Variables written:

| Variable | Value / generation | Kept or derived | Used by |
|---|---|---|---|
| `ENV` | `dev` | kept | all |
| `APP_VERSION` | `git rev-parse --short HEAD` or `dev` | derived | images, OTel `service.version` |
| `COMPOSE_PROJECT_NAME` | `payrun` | kept | Compose |
| `APP_PORT_BASE` | `4100` | kept | ports below |
| `WEB_PORT`, `GRAFANA_PORT`, `MAILPIT_PORT` | base+0, +1, +2 (4100, 4101, 4102) | derived | Compose (Compose can't add numbers) |
| `IDENTITY_PORT`, `PAYROLL_PORT`, `PAYOUT_WORKER_PORT`, `BANK_STUB_PORT` | base+10…+13 (4110–4113) | derived | `compose.test.yaml`, tests |
| `TEMPO_PORT`, `LOKI_PORT`, `PROMETHEUS_PORT`, `ALERTMANAGER_PORT`, `TOXIPROXY_PORT` | base+30…+34 (4130–4134) | derived | `compose.test.yaml`, tests |
| `ALERT_SINK_PORT` | base+35 (4135; R31) | derived | `compose.test.yaml`, alert drills |
| `COMPANY_ID` | fixed dev UUID `0193a000-0000-7000-8000-000000000001` (C28; the same value in the event-schema examples, tests and every design) | kept | every service, payroll migration seed, identity bootstrap, seed, `mint_token.py` |
| `POSTGRES_PASSWORD` | `openssl rand -hex 24` | kept | Postgres superuser, bootstrap |
| `IDENTITY_DB_MIGRATOR_PASSWORD`, `IDENTITY_DB_PASSWORD` | hex 24 each | kept | bootstrap SQL, identity |
| `PAYROLL_DB_MIGRATOR_PASSWORD`, `PAYROLL_DB_PASSWORD` | hex 24 each | kept | bootstrap SQL, payroll |
| `PAYOUTS_DB_MIGRATOR_PASSWORD`, `PAYOUTS_DB_PASSWORD` | hex 24 each | kept | bootstrap SQL, payout-worker |
| `BANK_STUB_DB_MIGRATOR_PASSWORD`, `BANK_STUB_DB_PASSWORD` | hex 24 each | kept | bootstrap SQL, bank-stub |
| `POSTGRES_EXPORTER_PASSWORD` | hex 24 | kept | `90-monitoring.sql`, postgres-exporter |
| `REDIS_ADMIN_PASSWORD`, `REDIS_WEB_PASSWORD`, `REDIS_IDENTITY_PASSWORD`, `REDIS_PAYROLL_PASSWORD`, `REDIS_PAYOUT_WORKER_PASSWORD`, `REDIS_EXPORTER_PASSWORD` | hex 24 each | kept | Redis ACL, each client |
| `INTERNAL_JWT_PRIVATE_KEY` | Ed25519 PKCS8 PEM (`openssl genpkey -algorithm ed25519`) | kept | web (signing) |
| `INTERNAL_JWT_KID` | `dev-web-<YYYYMMDD>` | kept | web |
| `PAYOUT_WORKER_JWT_PRIVATE_KEY` | Ed25519 PKCS8 PEM | kept | payout-worker |
| `PAYOUT_WORKER_JWT_KID` | `dev-worker-<YYYYMMDD>` | kept | payout-worker |
| `IDENTITY_INTERNAL_JWT_PUBLIC_KEYS` | `{"<web kid>": {"iss": "web", "pem": "<web public PEM>"}}` (public key from `openssl pkey -pubout` of the kept private key) | derived | identity's `INTERNAL_JWT_PUBLIC_KEYS` |
| `PAYROLL_INTERNAL_JWT_PUBLIC_KEYS` | the web entry plus `{"<worker kid>": {"iss": "payout-worker", "pem": …}}` | derived | payroll's `INTERNAL_JWT_PUBLIC_KEYS` |
| `PAYROLL_FIELD_KEYS` | `{"dev-1": "<openssl rand -base64 32>"}` | kept | payroll |
| `PAYROLL_FIELD_ACTIVE_KID` | `dev-1` | kept | payroll |
| `AUDIT_IP_HASH_KEY` | `openssl rand -base64 32` | kept | identity |
| `SESSION_SECRET` | `openssl rand -base64 48` | kept | web |
| `BANK_STUB_API_KEY` | `openssl rand -hex 32` (no provider-like prefix, so secret scanners don't match it) | kept | bank-stub, payout-worker |
| `GRAFANA_ADMIN_PASSWORD` | hex 24 | kept | Grafana |
| `SMTP_HOST`, `SMTP_PORT` | `mailpit`, `1025` (no credentials in dev; `SMTP_USERNAME`/`SMTP_PASSWORD` are unset) | kept | identity (unprefixed infrastructure keys, C49) |
| `ALERT_PAGE_WEBHOOK_URL`, `ALERT_TICKET_WEBHOOK_URL` | `http://alert-sink:8080/page`, `http://alert-sink:8080/ticket` | kept | Alertmanager |
| `OTEL_TRACES_SAMPLER` | `parentbased_always_on` (C23; there is no `OTEL_TRACES_SAMPLER_ARG`) | kept | all |
| `SEED_OWNER_EMAIL` | `owner@payrun.example.com` | kept | bootstrap-owner, seed, e2e |
| `SEED_OWNER_PASSWORD`, `SEED_APPROVER_PASSWORD`, `LOAD_USER_PASSWORD` | 20 random URL-safe characters each | kept | bootstrap-owner (`--password-env SEED_OWNER_PASSWORD`, R16), seed, e2e, k6 |
| `LOAD_USER_EMAIL` | `load@payrun.example.com` (role `payroll_admin` only; R22, C27) | kept | seed, k6 |

The Postgres, DB-role, exporter and Redis passwords use hex, so they never need escaping in ACL lines or connection strings.
The `.env` names `<SVC>_DB_PASSWORD`, `REDIS_<USER>_PASSWORD` and `<SVC>_INTERNAL_JWT_PUBLIC_KEYS` exist only in `.env`, Compose interpolation and the secret stores: each process receives them under its C1 name (`DB_PASSWORD`, `REDIS_PASSWORD`, `INTERNAL_JWT_PUBLIC_KEYS`; §7, §9).

#### `scripts/stack.sh`

`scripts/stack.sh <command> [args]`; sources `.env` (runs `dev-env.sh` first if `.env` is missing);
`COMPOSE="docker compose --env-file .env -f deploy/compose/compose.yaml"`, plus `-f deploy/compose/compose.test.yaml` when the test profile is on.

| Command | Does |
|---|---|
| `up [--profiles core,obs,test]` | Default profiles `core,obs,test` (the e2e set; `--profiles core` for a lighter dev stack). `$COMPOSE --profile … up -d --build --wait --wait-timeout 180`. On failure prints `ps` and the last 100 log lines of every unhealthy service, and exits 1. **Start-up timing (PAY-NFR-15):** then, for each of identity, payroll, payout-worker, bank-stub and web, reads `State.StartedAt` and the first passing health-check time from `docker inspect` and writes `ci-logs/startup.json` (`{service: seconds_to_ready}`); with `--check-startup` (set by `e2e.sh`) it exits 1 if any service took more than 20 s |
| `down [-v]` | `$COMPOSE --profile core --profile obs --profile test down --remove-orphans [-v]` |
| `logs [svc…]` | `$COMPOSE logs --no-color --timestamps [svc…]` |
| `ps` | `$COMPOSE ps` |
| `reset` | `down -v`, then `up` with the same profiles: every volume is recreated, which is how `bank_stub.transfers` is truncated (plan §6) |
| `seed [args]` | `uv run python scripts/seed.py [args]` against the running stack |
| `psql <role>` | opens psql in the postgres container as a role (passwords from `.env`), for debugging |

#### `scripts/export_openapi.py`

`uv run python scripts/export_openapi.py (<service> | --all) [--check]`
- For each of `identity`, `payroll`, `bank_stub` (payout-worker has no API), import `<pkg>.main:build_app`, build settings with `Settings.model_construct(...)` (placeholder values, no I/O), call `create_app(..., instrument=False)` and `app.openapi()`.
- Write `services/<dir>/openapi.json` as `json.dumps(spec, sort_keys=True, indent=2) + "\n"`.
- `--check` writes nothing and exits 1 if a file would change (used locally; CI uses `git diff --exit-code`).
- Services expose `build_app(settings) -> FastAPI`, with `app = build_app(load_settings(Settings))` in `main.py` only under the serve path, so importing `build_app` needs no env.

#### `scripts/seed.py` (PAY-609)

`uv run python scripts/seed.py [--skeleton] [--employees 10|500] [--base-url http://127.0.0.1:${WEB_PORT}] [--out PATH]`.
The identity, payroll and web LLDs own the endpoints it calls. All calls go through the BFF and Mailpit
APIs; no SQL. Progress goes to stderr; stdout carries only the JSON summary.

1. Refuses to run unless `ENV == "dev"` (exit 2, PAY-609). `--skeleton` with `--employees` is an argument error (exit 2).
2. Waits (polling, 60 s cap, else exit 3) for the web's `GET /api/readyz` (C4) and Mailpit's `/api/v1/info` (`MAILPIT_PORT`).
3. **Owner:** the `bootstrap-owner` one-shot has already created an **active** owner with `SEED_OWNER_PASSWORD` (`--if-empty --password-env`, R16, C27). The seed signs in as `SEED_OWNER_EMAIL` through `POST /api/auth/login` and keeps the session cookie. It never accepts an owner invite.
4. **Users:** invites (`POST /api/users/invite`) `approver@payrun.example.com` with roles `[approver]` and `LOAD_USER_EMAIL` with roles `[payroll_admin]` only (R22, C27), finds each invite in Mailpit (`/api/v1/search?query=to:…`, newest first), extracts the token from the `/accept#token=…` link and accepts it through `POST /api/auth/accept` with `SEED_APPROVER_PASSWORD` / `LOAD_USER_PASSWORD`. For each user the seed first tries to sign in with its password; when that works the user exists and is kept. Otherwise it invites; an invite answered 409 (a pending user left by an earlier run that stopped before accepting) makes it look the user up by email in `GET /api/users`, paging until `next_cursor` is null; the pending one gets `POST /api/invites/{pending_invite_id}/resend` and is then accepted. Both modes create these users: k6 smoke signs in as `LOAD_USER_EMAIL`, and the skeleton journey approves as the approver.
5. **Settings (full mode only):** `PUT /api/settings` with tax 2000 bps, retirement 500 bps and dual approval on (R22). `--skeleton` makes no settings PUT and runs on the migration's 0/0 seed (C33).
6. **Employees:** counts existing employees first, then creates the missing ones. Employee *i* is generated with `random.Random(SEED * 1000 + i)` (no factory library), so names and numbers are reproducible and a re-run creates nothing:
   - `--skeleton`: 5 salaried monthly employees (C33);
   - default: 10, salaried and hourly on both schedules (employees 6–10 include 4 biweekly and 3 hourly), one with an account ending `0000` (employee 9, closed account, for J4/J5);
   - `--employees 500`: the same first 10, then monthly employees up to 500 with every 10th hourly, for k6 (`FULL_SHAPES` and `_shape` in `seed.py`).
7. **Runs** (both modes, C33): ensures one monthly run for the current month (period start = the 1st, UTC) and one draft monthly run for the next month. The current-month run is created if missing, given 160 hours for each hourly payslip, and calculated as the owner when it is still `draft`; a run already past `calculated` is left as it is. Its ID is reported, so k6 smoke can read it.
8. **JSON summary** (stdout, and `--out PATH` when given; e2e writes `ci-logs/seed.json`):

```json
{
  "mode": "skeleton",
  "company_id": "0193a000-0000-7000-8000-000000000001",
  "users": {"owner": "<uuid>", "approver": "<uuid>", "load": "<uuid>"},
  "employees": 5,
  "runs": {"current": "<uuid>", "draft": "<uuid>"}
}
```

`mode` is `skeleton` or `full`; `employees` is the total after the run. A re-run prints the same IDs and counts (PAY-609).
Exit codes: 0 done; 1 an API step failed (logs the step, the HTTP status and the error `code`, never a body); 2 arguments or `ENV`; 3 the stack wasn't ready in 60 s.
Schemathesis and Playwright read `users.owner` from the summary; k6 smoke reads `runs.current`.

#### `scripts/mint_token.py` (PAY-609, C35)

A CLI plus an importable function, both on top of `pycommon.auth.TokenSigner`:

```python
def mint(aud: str, sub: str, roles: Sequence[str], *, issuer: Literal["web", "payout-worker"] = "web",
         cid: UUID | str = COMPANY_ID, name: str | None = None) -> str
```

- **Keys:** `issuer="web"` signs with `INTERNAL_JWT_PRIVATE_KEY` / `INTERNAL_JWT_KID`; `"payout-worker"` with `PAYOUT_WORKER_JWT_PRIVATE_KEY` / `PAYOUT_WORKER_JWT_KID`. They come from the process env, else from `./.env` (PEMs through `normalise_pem`). `COMPANY_ID` defaults the same way.
- **Claims** follow PAY-600 and R9: 60 s TTL, a fresh `jti`. A user `sub` (UUID) gets the given `roles`, `name` and a random `sid`; `sub="service:web"` gets `roles: []` and no `name` or `sid`; `issuer="payout-worker"` requires `sub="service:payout-worker"` and `roles: []`. Anything else raises `ValueError` (CLI: exit 2).
- **Dev and CI only:** refuses unless `ENV == "dev"` (exit 2), like the seed.
- **CLI:** `uv run python scripts/mint_token.py --aud payroll --sub <uuid|service:web|service:payout-worker> [--roles admin,approver] [--issuer web|payout-worker] [--cid UUID] [--name NAME]` prints the token and a newline on stdout and nothing else; nothing is logged. Playwright's test-only helper calls this CLI (C35); `scripts/ci/schemathesis_auth.py` imports `mint`.

#### `scripts/sync-k8s-assets.sh` (R32, C36)

`scripts/sync-k8s-assets.sh [--check]`. Kustomize can't load files outside a kustomization's root, and
the design runs it with no load-restrictor flag, so the script copies the shared assets byte for byte:

| Source (the only copy that is edited) | Copy |
|---|---|
| `deploy/compose/init/` (`*.sql`, `lib/service_schema.psql`, `lib/run-init.sh`) | `deploy/k8s/base/bootstrap/init/` |
| `deploy/compose/redis/redis.conf`, `entrypoint.sh` | `deploy/k8s/components/in-cluster-data/redis/` |
| `deploy/observability/collector.yaml` | `deploy/k8s/base/otel-collector/collector.yaml` |
| `deploy/observability/collector.prod.yaml` | `deploy/k8s/overlays/prod/collector.prod.yaml` |

It deletes copy files that no longer have a source. `--check` copies nothing and exits 1 listing every
file that differs or is missing. The copies are committed; `manifests.sh` runs the script and then
`git diff --exit-code` and `git status --porcelain` on the copy paths, so a stale copy fails CI.

#### `scripts/ci/*.sh`

All start with `source scripts/ci/lib.sh` (`set -euo pipefail`, `log()`, `group()` for GitHub log groups, `retry n cmd`, `compose()` as in `stack.sh`, and a trap that always runs the job's cleanup). Each runs the same way locally.

| Script | Does |
|---|---|
| `python.sh <member>` | `uv sync --locked --all-packages`; `ruff check <paths>`; `ruff format --check <paths>`; `mypy --strict <src> <tests>`; `pytest <member>/tests/unit --disable-socket --allow-unix-socket --cov=<pkg> --cov-branch`; `pytest <member>/tests/integration --cov=<pkg> --cov-branch --cov-append --cov-fail-under=<85 or 90> --cov-report=xml`. Member `platform` runs `tests/platform` (no coverage gate) |
| `web.sh` | `corepack enable` (when `pnpm` is missing); `pnpm install --frozen-lockfile`; `pnpm --filter web lint`; `pnpm exec prettier --check .`; `pnpm --filter web typecheck`; `pnpm --filter @payrun/api-client typecheck`; `pnpm --filter web test --coverage` (thresholds 80% in the Vitest config); `pnpm --filter web int` (the real-Redis integration tests through Testcontainers, so Docker is needed; web LLD §9.1); `pnpm --filter web build` with no secrets in the environment |
| `contract.sh` | `export_openapi.py --all`; `pnpm --filter @payrun/api-client generate`; `git diff --exit-code -- services/*/openapi.json packages/api-client`; per service, `oasdiff breaking <(git show origin/main:services/<svc>/openapi.json) services/<svc>/openapi.json --fail-on ERR` (skipped when `main` has no spec); `check-jsonschema --check-metaschema contracts/events/*.json`; `check-jsonschema --schemafile contracts/events/payout.requested.v1.json contracts/events/examples/payout.requested.v1.*.json`, and the same for `payout.requested.dlq.v1.json` and its examples (C21); every example's `data.company_id` equals the dev `COMPANY_ID` (C28) |
| `e2e.sh` | Takes `E2E_SUITE=skeleton\|full` (default `full`; `ci.yml` and `nightly.yml` set `full` since F6; C51, C70). Both suites: `dev-env.sh`; `stack.sh up --check-startup` (core, obs, test; PAY-NFR-15). **skeleton:** `seed.py --skeleton --out ci-logs/seed.json`, then `pnpm --filter web exec playwright test --grep @skeleton` (only `skeleton.spec.ts`). **full:** `seed.py --out ci-logs/seed.json` (the full seed, C33), then `seed.py --employees 500 --out ci-logs/seed.json`, then Playwright `--grep "@journeys\|@trace\|@a11y"` (J1–J5, role changes, errors, the trace test and axe). Playwright runs with `.env` and `SEED_JSON=ci-logs/seed.json` in its environment (`.env` already holds the test-only `PAYOUT_WORKER_JWT_PRIVATE_KEY` and `PAYROLL_PORT` for the blocked case, C44). Then, in **both** suites and **in this order** (C70): `pytest tests/platform/test_seed.py -m stack` (PAY-609); `k6.sh load/smoke.js` with `SEED_JSON=ci-logs/seed.json`; Schemathesis per service (below) — **after** the seed tests and k6, because its stateful phase deletes the still-calculated `runs.current` and changes seeded users and settings; `pytest tests/resilience -m smoke` (the PR fault smoke tests: slow DB, identity down; C38); `check_metrics.sh`. The seed tests and the fault smoke skip themselves when the stack or summary is missing, so the script fails unless their JUnit XML shows at least one test and no skips. On failure `stack.sh logs > ci-logs/compose.log`; always `stack.sh down -v` |
| `check_metrics.sh` | Queries Prometheus (`PROMETHEUS_PORT`) `/api/v1/series` (last hour, waiting up to `CHECK_METRICS_WAIT_S`, default 90 s) for each of the 26 required names: `http_server_request_duration_seconds_count`, `bff_request_duration_seconds_count`, `queue_messages_total`, `queue_consumer_lag`, `circuit_breaker_state`, `external_requests_total`, `payroll_payout_settlement_seconds_count`, `identity_emails_pending`, `rate_limiter_errors_total`, `redis_stream_length`, `pg_up`, `otelcol_exporter_sent_spans`, `outbox_unsent`, `outbox_oldest_age_seconds`, `payroll_payouts_stuck`, `payroll_runs_processing_oldest_age_seconds`, `payroll_attempts_needs_operator`, `payroll_late_settlements_total`, `identity_emails_failed_total`, `identity_email_oldest_pending_age_seconds`, `redis_memory_used_bytes`, `redis_memory_max_bytes`, `queue_dlq_messages`, `process_memory_usage_bytes`, `auth_signins_total`, `external_request_duration_seconds_count` (every series an alert, a gate or a drill reads; `auth_signins_total` and the external-call series appear once the e2e journeys have signed in and paid a run, which `e2e.sh` does before this check); fails listing the missing ones. Guards the metric-naming rule of §3.3 |
| `schemathesis_auth.py` | A Schemathesis auth provider (`@schemathesis.auth()`, R30, C35) that imports `scripts/mint_token.py`'s `mint` and mints a **fresh token per request** (tokens live 60 s). `aud` is the service under test (`PAYRUN_SERVICE`, set by `e2e.sh` per run). Identity's four public-flow routes get `sub="service:web"`, `roles: []` and `X-Client-IP: 127.0.0.1`; `/v1/internal/*` gets `issuer="payout-worker"`, `sub="service:payout-worker"`; every other route gets the owner (`users.owner` from `SEED_JSON`) with `[admin, approver, payroll_admin]` and `name="Owner"`. For bank-stub it sets `X-Api-Key: $BANK_STUB_API_KEY` instead |
| `images.sh` | **Trivy ignore policy first (C38):** every non-comment line of `.trivyignore` is a CVE ID followed by a comment `# reason: <text>; owner: <github handle>; exp:YYYY-MM-DD`; the script fails if a field is missing, if `exp` is in the past, or if the CVE is not also listed in `SECURITY_DEPENDENCIES.md`. Then `docker buildx bake -f deploy/docker/bake.hcl --set *.platform=linux/amd64 --load` (cache `type=gha`, one scope per image); `trivy image --severity HIGH,CRITICAL --exit-code 1 --ignore-unfixed=false --ignorefile .trivyignore payrun/<img>:ci` for the 5 images |
| `manifests.sh` | `scripts/sync-k8s-assets.sh`, then `git diff --exit-code` and an empty `git status --porcelain` on the copy paths (R32, C36); `kustomize build deploy/k8s/overlays/<env> \| kubeconform …` for dev, uat, prod (§9.6; no load-restrictor flag); `pytest tests/platform/test_k8s_policy.py`; `promtool check config`, `promtool check rules`, `promtool test rules` (in the pinned `prom/prometheus` image); `amtool check-config` (pinned `prom/alertmanager` image); `otelcol-contrib validate --config` for both Collector files; `docker compose -f … config -q` for both Compose files |
| `security.sh` | `uv export --frozen --all-packages --no-dev --no-hashes \| pip-audit -r /dev/stdin --strict` (accepted advisories via `--ignore-vuln` from `SECURITY_DEPENDENCIES.md`); `pnpm audit --audit-level high`; `gitleaks git --log-opts="<base>..HEAD" --redact --exit-code 1` |
| `nightly-load.sh` | stack up, `seed.py --employees 500 --out ci-logs/seed.json`, then each of `LOAD_SCENARIOS` (default `full soak settlement`) through `k6.sh`, every one running even when an earlier one fails: `load/full.js` (ramp to 30 req/s, held `FULL_HOLD`, default 10 min); `load/soak.js` (5 req/s for `SOAK_DURATION`, default 30m), followed by the **drift check** (p95 of the `window:last` simple requests within 20% of `window:first`, from the summary export) and the **resource check** `scripts/ci/soak_resources.py` over the soak window (PAY-NFR-05: RSS slope < 5% over the soak for identity, payroll, payout-worker and bank-stub, from `process_memory_usage_bytes` = `deriv × window / avg_over_time`; no `queue_consumer_lag` series both > 0 and rising, the `ConsumerLagGrowing` condition; a missing series fails); `load/settlement.js` (approves as `approver@payrun.example.com`, R22), cross-checked against Prometheus `histogram_quantile(0.95, payroll_payout_settlement_seconds)` ≤ 60 s over the step. Summaries go to `load/results/<scenario>.json` (uploaded); the job fails listing every failed scenario or check. The web exports no process-memory series, so its RSS is not in the resource check |
| `nightly-resilience.sh` | stack up, seed, `pytest tests/resilience` (all faults; fails on any skip). Each test checks its HLD §4 row's log event (in `docker compose logs`) and metric (an instant query on the stack's Prometheus, `Stack.prom`, polled for up to 90 s because of the 15 s export and scrape intervals). `tests/resilience/_stack.py` reads the payout timing (`PAYOUT_CLAIM_MIN_IDLE_MS`, `PAYOUT_MAX_DELIVERIES`, `BANK_TOTAL_DEADLINE_S`, `PAYOUT_REPUBLISH_AFTER_S`, `MAINTENANCE_INTERVAL_S`) from `docker compose config --format json`, so the waits follow `x-payout-timing` |
| `nightly-e2e.sh` | `E2E_SUITE=full e2e.sh` `E2E_REPEAT` (default 3) times in a row, a fresh stack each time; another `E2E_SUITE` in the environment is overridden with a warning; each run's log and Playwright report are kept (`ci-logs/e2e-run-<n>.log`, `ci-logs/playwright-run-<n>`) |
| `nightly-schemathesis.sh` | Schemathesis with `--max-examples 500` per service |
| `nightly-audit.sh` | `security.sh` with fresh advisory DBs, plus `trivy image` of the freshly built images |
| `report_failure.sh <job>` | Opens or updates the nightly issue (§11.2) |

Schemathesis command (e2e): `SCHEMATHESIS_HOOKS=scripts.ci.schemathesis_auth schemathesis run services/<svc>/openapi.json --url http://127.0.0.1:<SVC_PORT> --checks all --max-examples 50` (hooks are loaded as a module; flag names are checked against the pinned Schemathesis version at scaffold time).

## 4. Configuration

Keys owned by the platform (pycommon base settings, shared timing, infrastructure). Service-specific
keys (`AUTH_LOGIN_LIMIT_*`, `SESSION_*`, `BANK_STUB_*`, …) are in the service LLDs; their values in
Compose and Kubernetes are listed in §7 and §9.

**Naming (C1, D1):** the names below are authoritative for every Python service, runtime and migrate
process alike, in Compose, Kubernetes, Testcontainers and `.env.example`. No process reads `DATABASE_URL`,
`MIGRATOR_DATABASE_URL` or `REDIS_URL`, and no service prefixes these keys. The web uses the same Redis
names (`REDIS_HOST`, `REDIS_PORT`, `REDIS_USERNAME=web`, `REDIS_PASSWORD`; C12).

| Key / env var | Type | Default (dev / uat / prod) | Requirement |
|---|---|---|---|
| `ENV` | `dev\|uat\|prod` | dev / uat / prod | PAY-605 |
| `APP_VERSION` | str | git SHA / image tag / image tag | NFR-10 (`service.version`) |
| `LOG_LEVEL` | `DEBUG\|INFO\|WARNING\|ERROR`, any case (C24) | INFO / INFO / INFO | NFR-07 |
| `DEBUG`, `DOCS_ENABLED` | bool | false, true / false / false (refused if true) | PAY-605 |
| `COMPANY_ID` | UUID | `0193a000-0000-7000-8000-000000000001` (C28) / per env / per env | PAY-200, 600 |
| `DB_HOST`, `DB_PORT`, `DB_NAME` | str, int, str | postgres, 5432, payrun / managed endpoint | PAY-606 |
| `DB_USER`, `DB_PASSWORD`, `DB_SCHEMA` | str, secret, str | `<schema>_app` (runtime) or `<schema>_migrator` (migrate, bootstrap-owner) per C2; password from `<SVC>_DB_PASSWORD` / `<SVC>_DB_MIGRATOR_PASSWORD` | PAY-606 |
| `DB_STATEMENT_TIMEOUT_MS` / `DB_POOL_TIMEOUT_S` | int / float | 2000 / 2 everywhere | PAY-605, plan §2.2 |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | int | 10 / 5 by default; payout-worker and bank-stub 20 / 5 (their designs); tuned after load tests | NFR-05 |
| `REDIS_HOST`, `REDIS_PORT`, `REDIS_USERNAME`, `REDIS_PASSWORD` | str, int, str, secret | redis, 6379, the service's ACL user (§2.2), password from `REDIS_<USER>_PASSWORD` / managed endpoint | PAY-607 |
| `REDIS_SOCKET_TIMEOUT_MS` / `PAYOUT_XREAD_BLOCK_MS` | int | 500 / 5000 everywhere | plan §2.2 |
| `INTERNAL_JWT_PUBLIC_KEYS` | JSON keyset | from `<SVC>_INTERNAL_JWT_PUBLIC_KEYS` / ConfigMap / ConfigMap | PAY-600 |
| `MAX_BODY_BYTES` | int | 262144 | PAY-604 |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD` | str, int, str, secret | mailpit, 1025, unset, unset / in-cluster Mailpit / provider via ExternalSecret (unprefixed infrastructure keys, read by identity; C49) | PAY-110 |
| `SERVICE_RATE_LIMIT_USER` / `_WORKER` | int per min | 600 / 6000 (test env: 1000000 / 1000000) | PAY-603 |
| `BREAKER_FAILURES` / `BREAKER_RESET_S` | int / float | 5 / 30 | plan §2.2 |
| `MAINTENANCE_INTERVAL_S` | int | 60 | PAY-608 |
| `RETENTION_OUTBOX_DAYS` / `RETENTION_PROCESSED_EVENTS_DAYS` / `RETENTION_TOKENS_DAYS` / `RETENTION_BANK_DETAILS_DAYS` / `IDEMPOTENCY_TTL_H` | int | 7 / 30 / 7 / 30 / 24 | PAY-608 |
| `STREAM_MAXLEN` | int | 100000 | PAY-402 |
| `PAYOUT_MAX_DELIVERIES` | int | 5 (shared, `payout-timing`) | PAY-404, 408 |
| `PAYOUT_CLAIM_MIN_IDLE_MS` | int | 60000 (shared) | PAY-408, 605 |
| `PAYOUT_MARK_REJECTED_MIN_WAIT_S` | int | 120 (shared; C48) | PAY-502, 605 |
| `PAYOUT_REPUBLISH_AFTER_S` / `PAYOUT_REPUBLISH_MAX_BACKOFF_S` / `PAYOUT_REPUBLISH_MAX` | int | 360 / 900 / 10 (shared; README resolution R1) | PAY-407, 605 |
| `BANK_TOTAL_DEADLINE_S` / `PAYOUT_PAYROLL_CALL_DEADLINE_S` | float | 15 / 5 (shared) | PAY-404, 605 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | URL | `http://otel-collector:4317` (Python), `:4318` (web) | NFR-09, 10 |
| `OTEL_TRACES_SAMPLER` | str | `parentbased_always_on` everywhere (R27, C23); no `OTEL_TRACES_SAMPLER_ARG`; prod sampling is the Collector's `tail_sampling` | plan §11 |
| `OTEL_METRIC_EXPORT_INTERVAL` | ms | 15000 | NFR-10 |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | s (Python gRPC exporter only; the web's JS exporter is not configured by it) | 3 in Compose `x-py-env` and the four Python Deployments (identity, payroll, payout-worker, bank-stub); unset (SDK default 10) in the migration and bootstrap Jobs, which flush once on exit and have no grace budget (C67) | PAY-408, NFR-02 |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | str | `http` (Dockerfile, Compose, K8s, and `otel.py` fallback) | NFR-10 |
| `PYCOMMON_APP` / `PORT` | str / int | `<pkg>.main:app` / 8000 (Python Dockerfile); the web container uses `PORT=3000` (C4) | — |
| `APP_PORT_BASE` | int | 4100 / n/a / n/a | PAY-NFR-13 |

## 5. Logging and metrics (pycommon)

Service LLDs add their domain events and business metrics. These come from pycommon in every service.

| Event / metric name | Level / type | Fields / labels | When | Requirement |
|---|---|---|---|---|
| `http.request` | info (warning on 5xx) | method, route, status, duration_ms, caller | every non-health request | NFR-07, 10 |
| `http.error` / `http.unhandled` | info, warning / error | code, status / error_type + traceback | AppError / unhandled exception | PAY-602 |
| `auth.token.rejected` | warning | reason, kid, iss | JWT verification failed | PAY-600 |
| `auth.forbidden` | info | route, subject_kind or missing_roles | 403 by the C30 rule | PAY-601 |
| `settings.invalid` | error | fields | settings validation failed; exit 2 | PAY-605 |
| `startup.check_failed` | error (uat/prod) / warning (dev) | setting, reason | a startup check fails; exit 2 in uat/prod (C29), and in every env for a missing JWT keyset (C63) | PAY-605 |
| `otel.sampler.overridden` | warning | sampler | `OTEL_TRACES_SAMPLER` isn't `parentbased_always_on` | plan §11 |
| `metrics.gauge_query_failed` | warning | gauge, error_type | an `observable_gauge` callback raised | plan §2.1 |
| `service.started` / `service.stopped` | info | version, env | lifespan | — |
| `background.task.crashed` | error | task, error_type | a background task raised | — |
| `health.check_failed` | warning | check, error_type | `/readyz` failure (rate-limited) | PAY-605 |
| `db.timeout` / `db.unavailable` | warning | operation | DB timeout / connection failure | PAY-605 |
| `redis.unavailable` | warning | operation | Redis error not handled by the caller | plan §2.1 |
| `outbox.relay.published` | info | count, duration_ms | a `RelayLoop` batch sent at least one row (C63) | PAY-402 |
| `outbox.relay.error` | error | error_type, batch_size, failed, backoff_ms | `RelayLoop` XADD or commit failed | PAY-402 |
| `external.request.retry` / `external.request.failed` | warning | provider, operation, attempt, reason, delay_ms | resilient client | plan §9 |
| `external.breaker.opened` / `.half_open` / `.closed` | warning / info / info | target | breaker transition | plan §9 |
| `ratelimit.backend_error` | warning (≤ 1 per 10 s) | component, fail_mode | Redis failed in the limiter | PAY-603 |
| `maintenance.task.completed` / `maintenance.task.failed` / `maintenance.skipped` | info / error / debug | task, affected, duration_ms / task, error_type / — | each task per tick; lock not held (C44) | PAY-608 |
| `http_server_request_duration_seconds` | histogram (auto) | `http_route`, `http_request_method`, `http_response_status_code` | every request | NFR-10 |
| `db_client_connections_usage` | gauge (auto) | `state` | SQLAlchemy pool | NFR-10 saturation |
| `asyncio_event_loop_lag_seconds` | gauge | — | 1 s ticker | NFR-10 saturation |
| `external_requests_total` | counter | `provider`, `operation`, `outcome` | per attempt | plan §9, 11 |
| `external_request_duration_seconds` | histogram | `provider`, `operation` | per attempt | plan §9 |
| `circuit_breaker_state` | gauge, unit `{state}` (0 closed, 1 half-open, 2 open; C63) | `target` | breaker | plan §11 |
| `rate_limiter_errors_total` | counter | `component` | Redis failure in the limiter, or `RateLimiter.record_error()` (C63) | PAY-603 |
| `rate_limited_total` | counter | `scope` | request denied, or `RateLimiter.record_denied(scope)` (C63) | PAY-603 |
| `maintenance_runs_total` / `maintenance_duration_seconds` | counter / histogram | `task`, `outcome` / `task` | per task per tick | PAY-608 |

All metrics carry `service_name` and `deployment_environment_name` from the resource (§8.1). No
label ever holds a user id, email, request id, raw path or error message.

**Service-owned metrics the platform assets depend on** (rules, dashboards and `check_metrics.sh`; the
owning LLD defines them, and these names and labels are the contract):
- payout-worker: `queue_messages_total{stream, group, outcome}` with `outcome` ∈ `ok`, `retry`, `dlq`, pre-initialised with `add(0)` for each (C21); `queue_processing_seconds`, `queue_consumer_lag`, `queue_dlq_messages`, `payouts_bank_transfers_total{outcome, reason}`;
- payroll: `payroll_*` and `outbox_unsent` / `outbox_oldest_age_seconds` (payroll LLD §7.2, C18);
- identity: `auth_signins_total{outcome}` with `outcome` ∈ `success`, `failure`, `rate_limited` (C19); `identity_emails_failed_total` (counter, C20, R25), `identity_emails_pending`, `identity_email_oldest_pending_age_seconds`;
- web: `bff_request_duration_seconds{route, status}` (route templates, status as the numeric code) and the BFF's own counters (web LLD §7.2).

## 6. Tests

### 6.1 pycommon (gate: ≥ 90% line coverage, branch coverage on; unit + integration combined)

Unit tests run with `--disable-socket --allow-unix-socket`; integration tests use the
`pycommon.testing` containers. Each test cites its requirement in its docstring. Fake credentials are
generated at runtime (`secrets.token_urlsafe`), never committed as literals (testing.md §9, C63).

| Test file | Covers |
|---|---|
| `tests/unit/test_settings.py` | Precedence env > `.env` > defaults; the C1 names are read unprefixed (`DB_HOST` … `REDIS_PASSWORD`) and `DATABASE_URL`/`REDIS_URL` are ignored; `LOG_LEVEL=info`, `Info` and `INFO` all load as `INFO`, `verbose` fails (C24); `SettingsError` lists field names and never values (a secret placed in an invalid field doesn't appear in the message), and `load_settings` exits 2; `normalise_pem`; `db_url` quoting; `secret_fields` |
| `tests/unit/test_logging.py` | JSON line has every required field; `trace_id`/`span_id` inside a span (in-memory exporter); `redact` masks each of the 7 PAY-NFR-07 keys and the extra ones, case-insensitively, nested in dicts and lists, plus JWT / Bearer / PEM values under innocent keys; depth cap; `user_text` length outside dev; uvicorn access log silenced; OTLP handler gets redacted attributes (NFR-07) |
| `tests/unit/test_otel.py` | Resource attributes; `OTEL_SEMCONV_STABILITY_OPT_IN` set before instrumentation import; the sampler is `ParentBased(ALWAYS_ON)` when `OTEL_TRACES_SAMPLER` is unset or `parentbased_always_on`, and another value logs `otel.sampler.overridden` (C23); a TestClient request yields `http.server.request.duration` with `http.route` (the stable name, checked with `InMemoryMetricReader`); `url.query` dropped from server and client spans; Redis hook records no arguments; histogram buckets applied; `observable_gauge` reports the callback's value at collection, and a raising callback yields no observation and logs `metrics.gauge_query_failed` (R26, C18); unit `"1"` is refused and `circuit_breaker_state` has unit `{state}` (C63); a re-registered gauge uses the latest callback; an import-time counter reports per test without global resets, and histogram deltas add up (C61, C63); traceparent inject/extract round trip, `new_traceparent` format (unsampled flag), and invalid input → empty context |
| `tests/unit/test_errors.py` | Each handler row of §3.4: status, code, body shape with `request_id`, no traceback or exception text in the body, 422 details without input values, `Retry-After` on 429 (PAY-602); a bare `OSError`/`TimeoutError` from a route is 500, while one raised inside the driver is 503 (C63); SQLSTATE 22021 is 422 `validation_error`, in the handler and in `map_db_errors` (C63) |
| `tests/unit/test_jwt.py` | PAY-600: valid web and worker tokens pass; `name` round-trips to `Principal.name` and is `None` when absent (C10); 401 `invalid_token` for missing header, malformed, unknown kid, `alg` ≠ EdDSA (including `none` and HS256 with the public key as secret), tampered signature, expired, iat in the future, wrong aud, missing each required claim, lifetime > 60 s; **a token signed with the worker key but `iss=web` is rejected (`issuer_mismatch`)**; 5 s leeway accepted and 6 s rejected (`FakeClock`); the token never appears in captured logs; `Keyset.from_json` rejects bad JSON, unknown iss, non-Ed25519 PEM; `TokenSigner.mint` claims |
| `tests/unit/test_auth_deps.py` | The C30 matrix: route issuer not allowed → 401 `invalid_token` (web token on an internal route; worker token on a user route; PAY-411); `service:web` on a user route → 403 `forbidden_role`; a user token on a `service:web`-only route → 403; the issuer check runs first, so a web user token on a worker-only route is 401, not 403; missing role → 403 `forbidden_role`; `any_role` vs all roles; logs carry no token, `name` or `sid`; `Principal.caller_key` (PAY-601, 411) |
| `tests/unit/test_health.py` | `/livez` always 200; `/readyz` 200 / 503 with per-check status and no error text; a hanging check times out at 1 s; checks run concurrently; health routes have no auth |
| `tests/unit/test_db.py` | `create_engine` arguments (statement timeout, search path, pool timeout, pre-ping); `map_db_errors` maps each driver error to `DbTimeout` / `DbUnavailable`; `advisory_lock_key` is stable and signed 64-bit; `new_id` is version 7 and time-ordered |
| `tests/unit/test_streams_unit.py` | `Envelope.new` fields and `extra="forbid"`; `traceparent` is required (a missing or malformed one fails validation) and is filled from the active span or `new_traceparent()` (C22); `StreamProducer.publish` sends exactly `event_id`, `type`, `envelope` with `MAXLEN ~` (fake Redis port; R7); `RelayLoop` with a fake `OutboxRepository`: sent items marked sent, failed items marked failed with the class name, backoff doubles with jitter up to 5 s and resets after a clean batch, `wake()` skips the poll wait, a full batch loops at once; `outbox.relay.published` {count, duration_ms} after a batch with sent items, and not after an empty one (C63) |
| `tests/unit/test_http.py` | Every behaviour line of §3.10 with respx and `FakeClock`: per-attempt timeouts; total deadline across retries; retries only for idempotent methods or with an `Idempotency-Key` (same key on every attempt); 429/502/503/504 retried, other 4xx not; `Retry-After` honoured or abandoned when past the deadline; full-jitter bounds; `auth_headers` called per attempt; `request` mapping to each typed error; `request_raw` returns 4xx/5xx unmapped; metrics labels per outcome; no body or header in logs |
| `tests/unit/test_breaker.py` | closed → open after 5 consecutive failures; 4xx/429 don't count; open fails fast without sending; half-open after 30 s allows exactly one trial; trial success closes, failure re-opens; `circuit_breaker_state` values and transition logs |
| `tests/unit/test_ratelimit_unit.py` | Key layout and subject hashing (the `caller` key format of §3.11); window arithmetic and `retry_after_s`; worker vs user limit selection in `caller_rate_limit`; `service:web` is exempt by default and `exempt_subs` overrides it (C17); `record_denied` and `record_error` count on the shared series and log at most once per 10 s (C63) |
| `tests/unit/test_startup_checks.py` | PAY-605: each check fails and passes on crafted settings; missing secret; each `DEV_DEFAULTS` value; `dev-`/`test-` kids in the keyset, `*_KID` fields and `PAYROLL_FIELD_KEYS`; debug and docs on; bank URL pointing at `bank-stub`, `localhost` or a placeholder in prod (accepted in uat); `check_claim_idle` boundary (35 s fails, 35.001 s passes); `check_republish_after` boundary (315 fails, 315.001 passes), with the R1 default 360 passing and the old 300 failing; `BANK_BASE_URL=https://bank-provider.invalid` refused in prod; uat/prod exit 2 with one `startup.check_failed` line per failure naming the setting and never the value (C29); dev logs warnings and continues |
| `tests/unit/test_maintenance_unit.py` | Interval with jitter; first tick delay; a failing task doesn't stop the next; log names `maintenance.task.completed`, `maintenance.task.failed`, `maintenance.skipped` (C44) |
| `tests/unit/test_app.py` | `create_app` assembly: docs off unless enabled; `app.openapi()` works with docs off; request id accepted / regenerated / echoed in `x-request-id`, `x-trace-id` set; body limit 413 by `Content-Length` and by streamed size; every router except health requires a JWT; `public_routers` don't; startup checks run before any connection (a failing check means no engine is created); a missing JWT keyset exits 2 even in dev (C63); background tasks start and stop within the timeout; a crashing background task is restarted with backoff |
| `tests/unit/test_serve.py` | `pycommon.serve` builds the uvicorn config from env (access log off, server header off) |
| `tests/integration/test_db_pg.py` | Statement timeout → 503 `db_timeout` via a `pg_sleep(3)` route; pool exhaustion → `db_timeout` after 2 s; DB stopped → `db_unavailable` fast, and recovery without stuck connections once restarted; `migrations_check` detects a head mismatch; `run_alembic_env` puts the version table in the schema and revokes runtime writes on it |
| `tests/integration/test_streams.py` | With real Redis and the ACL users: the `payroll` user publishes with `MAXLEN ~` and the entry has exactly the three fields; `XREADGROUP` as `payout-worker` reads the envelope back and `context_from_traceparent` continues the producer's trace (same trace id); a client from `create_redis(socket_timeout_ms=5500)` survives `BLOCK 5000` while the default client times out at 500 ms |
| `tests/integration/test_relay_loop.py` | Against a throwaway outbox table the **test** creates (pycommon ships none): two loops on one table never send a row twice (SKIP LOCKED); Redis stopped → rows stay unsent with `relay_attempts` rising, then the loop catches up after recovery; batch of 100 then an immediate loop; **relay crash mid-batch** (HLD §4 "Outbox relay crash"): one loop claims a batch and dies before publishing, a second loop skips the locked rows until the dead connection is terminated, then sends each row exactly once (one `relay_attempts` each), `outbox.relay.error` is logged and the oldest-unsent age returns to 0 |
| `tests/integration/test_ratelimit.py` | PAY-603: two limiter instances share counts; 429 with `Retry-After` past the limit; Redis stopped → `open` allows and counts `rate_limiter_errors_total`, `closed` raises 503 `service_unavailable` |
| `tests/integration/test_maintenance.py` | Two runners on one database: only one runs per tick (advisory lock); the lock is released after the tick and after a killed connection; a test task's affected count is logged in `maintenance.task.completed` (PAY-608; each service's own integration suite proves its purge windows) |
| `tests/integration/test_secret_not_logged.py` | A full request through `create_app` with every secret set to a recognisable random value: none of them appears in captured stdout logs or exported spans (NFR-07) |

### 6.2 Platform assets (`tests/platform`, CI matrix member `platform`)

| Test file | Covers |
|---|---|
| `test_bootstrap_sql.py` | PAY-606: the init SQL runs in a fresh Postgres; each runtime role gets `permission denied` on every other schema and can't `CREATE` in its own; each migrator owns its schema; default privileges give the runtime role DML on a table the migrator creates; `public` has no rights; `postgres_exporter` has `pg_monitor` and can't read any table; an unset or empty password env var fails with the variable's name and no value; re-running a file succeeds and rotates a changed password; a re-run leaves `<schema>.alembic_version` SELECT-only for the runtime role (C63); after identity's migrations and a full re-run, `identity_app` still gets `permission denied` for `UPDATE` and `DELETE` on `identity.audit_log` and can `INSERT` and `SELECT` (C50), and the files run cleanly when the table doesn't exist yet; `log_statement` is `none`. identity's own `services/identity/tests/integration/test_grants.py` also asserts that `audit_log` UPDATE is denied after a bootstrap re-run (C50) |
| `test_redis_acl.py` | PAY-607: `entrypoint.sh` output loads; every user can `PING` (R12); `EVAL` and `FCALL` give `NOPERM` for every user but `default` (C3); web and identity get `NOPERM` on `XADD stream:payout.requested`; payroll can `XADD` the stream but not read sessions; the worker can XREADGROUP, XACK, XAUTOCLAIM, XPENDING, XINFO, `XGROUP CREATE … MKSTREAM` on an absent stream, and `XGROUP DELCONSUMER` (C27) on the stream, and only XADD and XINFO on the DLQ; the exporter can read stream info but can't write or read string keys (R12); `web`, `identity` and `payroll` get `NOPERM` for `SCAN`, `RANDOMKEY` and `KEYS` (C63); `--print` masks the hashes; the file contains no plaintext password; a missing env var aborts |
| `test_dev_env.py` | `dev-env.sh` in a temp dir: every §3.15 key present, file mode 0600, hex infrastructure passwords; output names keys and never values; `OTEL_TRACES_SAMPLER=parentbased_always_on` and no `OTEL_TRACES_SAMPLER_ARG` (C23); `COMPANY_ID` is the C28 value and equals `data.company_id` in every `contracts/events/examples` file; a second run changes no kept value; keysets parse with `Keyset.from_json` and their PEMs match the private keys; kids start with `dev-`; derived ports follow `APP_PORT_BASE` with no duplicated lines; `--k8s-dev-file` writes unquoted values that parse (keyset JSON, PEM with literal `\n`), without the Compose ports and with `SMTP_PORT` |
| `test_compose.py` | `docker compose config` for both files: every published port binds `127.0.0.1` and matches §7.1; every core service has a healthcheck; services depend on their migration with `service_completed_successfully` (PAY-NFR-13); **env contract (C1):** no service env has `DATABASE_URL`, `MIGRATOR_DATABASE_URL` or `REDIS_URL`, every Python service and migrate one-shot has `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_SCHEMA` with `DB_USER` matching C2, and every Redis client (web included) has `REDIS_HOST`, `REDIS_PORT`, `REDIS_USERNAME`, `REDIS_PASSWORD`; `OTEL_TRACES_SAMPLER=parentbased_always_on` everywhere (C23); the web has `PORT=3000`, port `${WEB_PORT}:3000` and a healthcheck on `/api/readyz` (C4); `bootstrap-owner` runs with `--if-empty` (R16) |
| `test_toxiproxy_config.py` | `toxiproxy.json` has exactly the four proxies and listen ports used by `compose.test.yaml` |
| `test_alert_sink.py` | POST /page and /ticket store payloads; GET returns them; DELETE clears; bounded at 500 per route |
| `test_rules_meta.py` | Every BFF SLI expression in `recording.yml` carries `route!~"/api/livez\|/api/readyz\|/api/telemetry"` (C4); no rule uses `outcome="failed"` for `auth_signins_total` (C19) or the gauge `identity_emails_failed` (C20); every alert in `alerts.yml` is one of the 17 in plan §11, has `severity` in {page, ticket, warning}, `summary`, and a `runbook_url` whose file exists under `docs/runbooks/`; every alert has at least one firing and one quiet `promtool` test case (NFR-10, 11) |
| `test_dashboards.py` | The five dashboard JSONs parse, have stable `uid`s, use datasource uids `prometheus`/`tempo`/`loki`, and every metric name they query is in the §5 table or a service LLD's list |
| `test_k8s_policy.py` | For each overlay's rendered output: every Deployment and Job has `runAsNonRoot`, `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false`, drops ALL, `seccompProfile: RuntimeDefault`, `automountServiceAccountToken: false`, resources and probes (Jobs: no probes); the web's probes are `/api/livez` (startup, liveness) and `/api/readyz` (readiness) on port 3000 (C4); no container env or ConfigMap holds `DATABASE_URL`, `MIGRATOR_DATABASE_URL` or `REDIS_URL` (C1); `OTEL_TRACES_SAMPLER` is `parentbased_always_on` in every overlay (C23); every Python Deployment (and Compose's `x-py-env`) sets `OTEL_EXPORTER_OTLP_TIMEOUT` ≤ 3 (C67); a default-deny policy exists; bank-stub is absent from prod; only payout-worker has an egress rule to the bank; **prod's payout-worker Deployment has `replicas: 0`** (R14, C32); the Collector has 1 replica in every overlay (§8.1); uat and prod use ExternalSecrets and have no plaintext Secret values (R29; PAY-NFR-13, ADR 0011) |
| `test_mint_token.py` | PAY-609, C35: `mint()` tokens verify with the `.env` keysets for each issuer; claims per PAY-600 (user: roles, `name`, `sid`; `service:web`: no `name`, no `sid`; worker: `roles: []`); a bad issuer/sub pair raises; the CLI prints only the token and exits 2 when `ENV` isn't dev |
| `test_soak_resources.py` | PAY-NFR-05: `scripts/ci/soak_resources.py` against a fake Prometheus: every Python service under 5% RSS growth and no lag pass; 5% itself fails; a service with no `process_memory_usage_bytes` series fails; lag fails only when both > 0 and rising; no `queue_consumer_lag` series fails; exit codes 0 / 1 / 2 (window < 120 s, Prometheus unreachable). The PromQL itself runs in promtool (pinned `prom/prometheus` image) on synthetic series: a 7.7% RSS climb and a rising lag are flagged, flat series are not |
| `test_sync_k8s_assets.py` | R32, C36: in a temp copy of the repo, `sync-k8s-assets.sh --check` passes after a sync and fails (exit 1, listing the file) after a source edit, a copy edit or a deleted source |
| `test_seed.py` (marker `stack`; run by `e2e.sh`, skipped in the `platform` matrix member) | PAY-609: against the running stack, the JSON summary validates (keys and types of §3.15, `company_id` = `COMPANY_ID`); a second run prints the same IDs and counts; `ENV=uat` exits 2; `LOAD_USER_EMAIL` has exactly `[payroll_admin]` (C27); skeleton mode: 5 salaried monthly employees; full mode (10) and the 500 mode: the R22 settings (2000 / 500 bps, dual approval) and both schedules and pay types among the employees; in every mode `runs.current` is a calculated (or later) current-month monthly run and `runs.draft` a draft next-month one (C33, C54). `e2e.sh` fails the job if any of these is skipped |

Prometheus rule tests (`promtool test rules`) are described with the rules in §8.3.

## 7. Docker Compose (`deploy/compose/`; PAY-NFR-13)

### 7.1 Services per profile

| Service | Profile | Image | Depends on (condition) | Healthcheck | Host port (127.0.0.1) |
|---|---|---|---|---|---|
| `postgres` | core | `postgres:16-alpine` | — | `pg_isready -U postgres -d payrun` | — |
| `redis` | core | `redis:7-alpine` | — | `REDISCLI_AUTH=$REDIS_ADMIN_PASSWORD redis-cli ping` | — |
| `mailpit` | core | `axllent/mailpit` | — | `/mailpit readyz` | `MAILPIT_PORT` 4102 → 8025 |
| `migrate-identity` | core | identity | postgres (healthy) | one-shot | — |
| `migrate-payroll` | core | payroll | postgres (healthy) | one-shot | — |
| `migrate-payouts` | core | payout-worker | postgres (healthy) | one-shot | — |
| `migrate-bank-stub` | core | bank-stub | postgres (healthy) | one-shot | — |
| `bootstrap-owner` | core | identity | migrate-identity (completed_successfully) | one-shot | — |
| `identity` | core | identity | migrate-identity (completed), redis (healthy), mailpit (started) | `/readyz` | test: `IDENTITY_PORT` 4110 |
| `payroll` | core | payroll | migrate-payroll (completed), redis (healthy) | `/readyz` | test: `PAYROLL_PORT` 4111 |
| `bank-stub` | core | bank-stub | migrate-bank-stub (completed) | `/readyz` | test: `BANK_STUB_PORT` 4113 |
| `payout-worker` | core | payout-worker | migrate-payouts (completed), redis (healthy), payroll (healthy), bank-stub (healthy) | `/readyz` | test: `PAYOUT_WORKER_PORT` 4112 |
| `web` | core | web | identity (healthy), payroll (healthy), redis (healthy), bootstrap-owner (completed) | `node -e "fetch('http://127.0.0.1:3000/api/readyz')…"` (C4) | `WEB_PORT` 4100 → 3000 |
| `otel-collector` | obs | `otel/opentelemetry-collector-contrib` | — | `healthcheck` extension `:13133` (via the collector's `healthcheckv2`; image has no shell, so `--wait` relies on `start_period`) | — |
| `tempo` | obs | `grafana/tempo` | — | `wget -qO- localhost:3200/ready` | test: `TEMPO_PORT` 4130 → 3200 |
| `loki` | obs | `grafana/loki` (3.x) | — | `wget -qO- localhost:3100/ready` | test: `LOKI_PORT` 4131 → 3100 |
| `prometheus` | obs | `prom/prometheus` | — | `wget -qO- localhost:9090/-/ready` | test: `PROMETHEUS_PORT` 4132 → 9090 |
| `alertmanager` | obs | `prom/alertmanager` | alert-sink (healthy) | `wget -qO- localhost:9093/-/ready` | test: `ALERTMANAGER_PORT` 4133 → 9093 |
| `alert-sink` | obs | the pinned Python base image + `alert_sink.py` | — | `python -c` GET `/healthz` | test: `ALERT_SINK_PORT` 4135 → 8080 (R31) |
| `grafana` | obs | `grafana/grafana` | prometheus, tempo, loki (healthy) | `wget -qO- localhost:3000/api/health` | `GRAFANA_PORT` 4101 → 3000 |
| `postgres-exporter` | obs | `quay.io/prometheuscommunity/postgres-exporter` | postgres (healthy) | — (scrape `up`) | — |
| `redis-exporter` | obs | `oliver006/redis_exporter` | redis (healthy) | — (scrape `up`) | — |
| `toxiproxy` | test | `ghcr.io/shopify/toxiproxy` | postgres, redis (healthy) | `/toxiproxy-cli list` | test: `TOXIPROXY_PORT` 4134 → 8474 |

- **No core service depends on an obs service.** Compose rejects a `depends_on` on a service outside the active profiles, and telemetry loss must never block the app; the OTel exporters retry and drop quietly when the Collector is absent.
- `bootstrap-owner` runs `python -m identity.bootstrap_owner --if-empty --email ${SEED_OWNER_EMAIL} --company-id ${COMPANY_ID} --password-env SEED_OWNER_PASSWORD` with the identity migrator credentials (PAY-100, PAY-609, R16). It creates an **active** owner with that password (dev only), and `--if-empty` exits 0 without changes when users already exist, so a second `stack.sh up` still satisfies `service_completed_successfully`. No exit-code wrapper.
- Migrate one-shots run `alembic -c /app/alembic.ini upgrade head` with the same C1 keys as the service, but `DB_USER=<schema>_migrator` and the migrator password, `restart: "no"`, and connect straight to `postgres` (never through Toxiproxy).
- `restart: unless-stopped` for long-running services; `read_only: true` with `tmpfs: [/tmp]` for the app containers (matching Kubernetes); `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`.
- Network: one bridge network `payrun`; services are reachable from the host only through the ports above, all bound to `127.0.0.1`.
- Volumes (named): `pgdata`, `redisdata`, `prometheus-data`, `tempo-data`, `loki-data`, `grafana-data`.

### 7.2 `compose.yaml` (abridged; every service follows these patterns)

```yaml
name: payrun

x-py-env: &py-env
  ENV: ${ENV:-dev}
  APP_VERSION: ${APP_VERSION:-dev}
  LOG_LEVEL: INFO                            # any case accepted (C24)
  COMPANY_ID: ${COMPANY_ID:?run scripts/dev-env.sh}   # C28
  DB_HOST: postgres
  DB_PORT: "5432"
  DB_NAME: payrun
  DB_STATEMENT_TIMEOUT_MS: "2000"
  DB_POOL_TIMEOUT_S: "2"
  REDIS_HOST: redis
  REDIS_PORT: "6379"
  REDIS_SOCKET_TIMEOUT_MS: "500"
  SERVICE_RATE_LIMIT_USER: "600"
  SERVICE_RATE_LIMIT_WORKER: "6000"
  BREAKER_FAILURES: "5"
  BREAKER_RESET_S: "30"
  MAINTENANCE_INTERVAL_S: "60"
  RETENTION_OUTBOX_DAYS: "7"
  RETENTION_PROCESSED_EVENTS_DAYS: "30"
  RETENTION_TOKENS_DAYS: "7"
  RETENTION_BANK_DETAILS_DAYS: "30"
  IDEMPOTENCY_TTL_H: "24"
  OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4317
  OTEL_EXPORTER_OTLP_TIMEOUT: "3"              # seconds (Python gRPC exporter); bounds the shutdown flush (C67)
  OTEL_EXPORTER_OTLP_PROTOCOL: grpc
  OTEL_TRACES_SAMPLER: parentbased_always_on   # C23: every service and environment; no _ARG
  OTEL_METRIC_EXPORT_INTERVAL: "15000"
  OTEL_SEMCONV_STABILITY_OPT_IN: http

x-payout-timing: &payout-timing
  PAYOUT_MAX_DELIVERIES: "5"
  PAYOUT_CLAIM_MIN_IDLE_MS: "60000"
  PAYOUT_REPUBLISH_AFTER_S: "360"            # README resolution R1
  PAYOUT_REPUBLISH_MAX_BACKOFF_S: "900"
  PAYOUT_REPUBLISH_MAX: "10"
  BANK_TOTAL_DEADLINE_S: "15"
  PAYOUT_PAYROLL_CALL_DEADLINE_S: "5"
  PAYOUT_MARK_REJECTED_MIN_WAIT_S: "120"     # C48

x-app: &app
  restart: unless-stopped
  read_only: true
  tmpfs: [/tmp]
  cap_drop: [ALL]
  security_opt: ["no-new-privileges:true"]
  networks: [payrun]
  profiles: [core]

x-py-health: &py-health
  test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=2).status == 200 else 1)"]
  interval: 5s
  timeout: 3s
  retries: 6
  start_period: 20s                          # PAY startup target: ready within 20 s after migrations

services:
  postgres:
    image: postgres:16-alpine                # pinned tag + digest at scaffold (§10.3)
    profiles: [core]
    environment:
      POSTGRES_DB: payrun
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?}
      IDENTITY_DB_MIGRATOR_PASSWORD: ${IDENTITY_DB_MIGRATOR_PASSWORD:?}
      IDENTITY_DB_PASSWORD: ${IDENTITY_DB_PASSWORD:?}
      PAYROLL_DB_MIGRATOR_PASSWORD: ${PAYROLL_DB_MIGRATOR_PASSWORD:?}
      PAYROLL_DB_PASSWORD: ${PAYROLL_DB_PASSWORD:?}
      PAYOUTS_DB_MIGRATOR_PASSWORD: ${PAYOUTS_DB_MIGRATOR_PASSWORD:?}
      PAYOUTS_DB_PASSWORD: ${PAYOUTS_DB_PASSWORD:?}
      BANK_STUB_DB_MIGRATOR_PASSWORD: ${BANK_STUB_DB_MIGRATOR_PASSWORD:?}
      BANK_STUB_DB_PASSWORD: ${BANK_STUB_DB_PASSWORD:?}
      POSTGRES_EXPORTER_PASSWORD: ${POSTGRES_EXPORTER_PASSWORD:?}
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./init:/docker-entrypoint-initdb.d:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d payrun"]
      interval: 3s
      retries: 20
    networks: [payrun]

  redis:
    image: redis:7-alpine
    profiles: [core]
    entrypoint: ["/bin/sh", "/usr/local/etc/redis/entrypoint.sh"]
    environment:
      REDIS_ADMIN_PASSWORD: ${REDIS_ADMIN_PASSWORD:?}
      REDIS_WEB_PASSWORD: ${REDIS_WEB_PASSWORD:?}
      REDIS_IDENTITY_PASSWORD: ${REDIS_IDENTITY_PASSWORD:?}
      REDIS_PAYROLL_PASSWORD: ${REDIS_PAYROLL_PASSWORD:?}
      REDIS_PAYOUT_WORKER_PASSWORD: ${REDIS_PAYOUT_WORKER_PASSWORD:?}
      REDIS_EXPORTER_PASSWORD: ${REDIS_EXPORTER_PASSWORD:?}
    volumes:
      - redisdata:/data
      - ./redis/redis.conf:/usr/local/etc/redis/redis.conf:ro
      - ./redis/entrypoint.sh:/usr/local/etc/redis/entrypoint.sh:ro
    tmpfs: ["/run/redis:mode=0700"]
    healthcheck:
      test: ["CMD-SHELL", "REDISCLI_AUTH=\"$$REDIS_ADMIN_PASSWORD\" redis-cli ping | grep -q PONG"]
      interval: 3s
      retries: 20
    networks: [payrun]

  migrate-payroll:
    <<: *app
    restart: "no"
    image: payrun/payroll:${APP_VERSION:-dev}
    build: { context: ../.., dockerfile: deploy/docker/python-service.Dockerfile, args: { PACKAGE: payroll, SERVICE_DIR: payroll } }
    command: ["alembic", "-c", "/app/alembic.ini", "upgrade", "head"]
    environment:
      <<: *py-env
      DB_USER: payroll_migrator
      DB_PASSWORD: ${PAYROLL_DB_MIGRATOR_PASSWORD:?}
      DB_SCHEMA: payroll
    depends_on:
      postgres: { condition: service_healthy }

  payroll:
    <<: *app
    image: payrun/payroll:${APP_VERSION:-dev}
    environment:
      <<: [*py-env, *payout-timing]
      DB_USER: payroll_app
      DB_PASSWORD: ${PAYROLL_DB_PASSWORD:?}
      DB_SCHEMA: payroll
      REDIS_USERNAME: payroll
      REDIS_PASSWORD: ${REDIS_PAYROLL_PASSWORD:?}
      INTERNAL_JWT_PUBLIC_KEYS: ${PAYROLL_INTERNAL_JWT_PUBLIC_KEYS:?}
      PAYROLL_FIELD_KEYS: ${PAYROLL_FIELD_KEYS:?}
      PAYROLL_FIELD_ACTIVE_KID: ${PAYROLL_FIELD_ACTIVE_KID:?}
      STREAM_MAXLEN: "100000"
      PAYOUT_STUCK_AFTER_S: "600"
      PAYOUT_MAX_ATTEMPTS: "5"
    depends_on:
      migrate-payroll: { condition: service_completed_successfully }
      redis: { condition: service_healthy }
    healthcheck: *py-health

  bootstrap-owner:
    <<: *app
    restart: "no"
    image: payrun/identity:${APP_VERSION:-dev}
    command: ["python", "-m", "identity.bootstrap_owner", "--if-empty",
              "--email", "${SEED_OWNER_EMAIL:?}", "--company-id", "${COMPANY_ID:?}",
              "--password-env", "SEED_OWNER_PASSWORD"]          # R16: active dev owner; exit 0 when users exist
    environment:
      <<: *py-env
      DB_USER: identity_migrator
      DB_PASSWORD: ${IDENTITY_DB_MIGRATOR_PASSWORD:?}
      DB_SCHEMA: identity
      SEED_OWNER_PASSWORD: ${SEED_OWNER_PASSWORD:?}
    depends_on:
      migrate-identity: { condition: service_completed_successfully }

  web:
    <<: *app
    image: payrun/web:${APP_VERSION:-dev}
    build: { context: ../.., dockerfile: apps/web/Dockerfile }
    environment:
      ENV: ${ENV:-dev}
      APP_VERSION: ${APP_VERSION:-dev}
      LOG_LEVEL: INFO
      NODE_ENV: production
      PORT: "3000"                                      # C4
      PUBLIC_BASE_URL: http://127.0.0.1:${WEB_PORT:?}    # C12
      IDENTITY_BASE_URL: http://identity:8000
      PAYROLL_BASE_URL: http://payroll:8000
      REDIS_HOST: redis                                 # C1, C12: no REDIS_URL
      REDIS_PORT: "6379"
      REDIS_USERNAME: web
      REDIS_PASSWORD: ${REDIS_WEB_PASSWORD:?}
      COMPANY_ID: ${COMPANY_ID:?}
      OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4318
      OTEL_TRACES_SAMPLER: parentbased_always_on
      # plus the keys of §7.3
    ports: ["127.0.0.1:${WEB_PORT:?}:3000"]
    healthcheck:
      test: ["CMD", "node", "-e", "fetch('http://127.0.0.1:3000/api/readyz').then(r=>process.exit(r.ok?0:1),()=>process.exit(1))"]
      interval: 5s
      timeout: 3s
      retries: 6
      start_period: 20s
    depends_on:
      identity: { condition: service_healthy }
      payroll: { condition: service_healthy }
      redis: { condition: service_healthy }
      bootstrap-owner: { condition: service_completed_successfully }
  # identity, bank-stub, payout-worker and the other migrate one-shots: same pattern as payroll (env per §7.3)

  alertmanager:
    image: prom/alertmanager
    profiles: [obs]
    entrypoint: ["/bin/sh", "-c"]
    command:
      - >-
        printf %s "$$ALERT_PAGE_WEBHOOK_URL" > /etc/alertmanager/secrets/page_url &&
        printf %s "$$ALERT_TICKET_WEBHOOK_URL" > /etc/alertmanager/secrets/ticket_url &&
        exec /bin/alertmanager --config.file=/etc/alertmanager/alertmanager.yml --storage.path=/alertmanager
    environment:
      ALERT_PAGE_WEBHOOK_URL: ${ALERT_PAGE_WEBHOOK_URL:?}
      ALERT_TICKET_WEBHOOK_URL: ${ALERT_TICKET_WEBHOOK_URL:?}
    volumes: ["../observability/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro"]
    tmpfs: ["/etc/alertmanager/secrets:mode=1777"]
    networks: [payrun]

volumes: { pgdata: {}, redisdata: {}, prometheus-data: {}, tempo-data: {}, loki-data: {}, grafana-data: {} }
networks: { payrun: {} }
```

### 7.3 Environment per service (beyond `x-py-env`)

Every Python service and one-shot, bank-stub included, gets `x-py-env`, so each has `COMPANY_ID` (C47:
`BaseServiceSettings` requires it) and the C1 DB keys. Only the keys below differ.

| Service | Env |
|---|---|
| identity | `DB_USER=identity_app`, `DB_PASSWORD=${IDENTITY_DB_PASSWORD}`, `DB_SCHEMA=identity`, `REDIS_USERNAME=identity`, `REDIS_PASSWORD=${REDIS_IDENTITY_PASSWORD}`, `INTERNAL_JWT_PUBLIC_KEYS=${IDENTITY_INTERNAL_JWT_PUBLIC_KEYS}`, `AUDIT_IP_HASH_KEY`, `SMTP_HOST=${SMTP_HOST}`, `SMTP_PORT=${SMTP_PORT}` (1025; C49), `AUTH_LOGIN_LIMIT_ACCOUNT=5`, `AUTH_LOGIN_LIMIT_IP=20`, `INVITE_TTL_H=72`, `RESET_TTL_H=1`, `EMAIL_MAX_ATTEMPTS=10`, `IDENTITY_PUBLIC_BASE_URL=http://127.0.0.1:${WEB_PORT}` (invite and reset links; C12); the rest per identity LLD |
| migrate-identity, bootstrap-owner | identity's keys with `DB_USER=identity_migrator`, `DB_PASSWORD=${IDENTITY_DB_MIGRATOR_PASSWORD}`; bootstrap-owner adds `SEED_OWNER_PASSWORD` (§7.2) |
| payroll | as in §7.2 (`PAYOUT_MAX_ATTEMPTS` is payroll's) |
| migrate-payroll | as in §7.2 |
| payout-worker | `DB_USER=payouts_app`, `DB_PASSWORD=${PAYOUTS_DB_PASSWORD}`, `DB_SCHEMA=payouts`, `REDIS_USERNAME=payout-worker`, `REDIS_PASSWORD=${REDIS_PAYOUT_WORKER_PASSWORD}`, `x-payout-timing`, `PAYOUT_CONCURRENCY=16`, `PAYOUT_XREAD_BLOCK_MS=5000`, `PAYOUT_CONSUMER_GC_IDLE_S=3600` (maintenance prunes idle consumers with no pending entries via `XGROUP DELCONSUMER`), `BANK_KEY_SAFE_WINDOW_S=86400`, `PAYOUT_PAYROLL_BASE_URL=http://payroll:8000`, `PAYOUT_PAYROLL_CONNECT_TIMEOUT_S=1`, `PAYOUT_PAYROLL_READ_TIMEOUT_S=2`, `PAYOUT_PAYROLL_MAX_RETRIES=3` (C46), `BANK_BASE_URL=http://bank-stub:8000`, `BANK_STUB_API_KEY`, `PAYOUT_WORKER_JWT_PRIVATE_KEY`, `PAYOUT_WORKER_JWT_KID`; no `PAYOUT_MAX_ATTEMPTS` (payroll's key; C46); `stop_grace_period: 75s` |
| migrate-payouts | `DB_USER=payouts_migrator`, `DB_PASSWORD=${PAYOUTS_DB_MIGRATOR_PASSWORD}`, `DB_SCHEMA=payouts` |
| bank-stub | `DB_USER=bank_stub_app`, `DB_PASSWORD=${BANK_STUB_DB_PASSWORD}`, `DB_SCHEMA=bank_stub`, `COMPANY_ID` (from `x-py-env`; C47), no Redis and no JWT keyset (`use_redis=False`, `use_jwt=False`), `BANK_STUB_API_KEY`, `BANK_STUB_FAILURE_RATE=0.05`, `BANK_STUB_LATENCY_MS=20` |
| migrate-bank-stub | `DB_USER=bank_stub_migrator`, `DB_PASSWORD=${BANK_STUB_DB_MIGRATOR_PASSWORD}`, `DB_SCHEMA=bank_stub` |
| web | the §7.2 block, plus `INTERNAL_JWT_PRIVATE_KEY`, `INTERNAL_JWT_KID`, `SESSION_SECRET`, `SESSION_COOKIE_NAME=sid`, `TRUSTED_PROXY_HOPS=0`, `BFF_RATE_LIMIT_SESSION=120`, `BFF_RATE_LIMIT_IP=300`, `SESSION_IDLE_TTL_S=43200`, `SESSION_ABS_TTL_S=604800`, `SESSION_ROLE_RECHECK_S=60`; the rest per web LLD. No `REDIS_URL` (C12) |
| postgres-exporter | `DATA_SOURCE_URI=postgres:5432/payrun?sslmode=disable`, `DATA_SOURCE_USER=postgres_exporter`, `DATA_SOURCE_PASS=${POSTGRES_EXPORTER_PASSWORD}` |
| redis-exporter | `REDIS_ADDR=redis://redis:6379`, `REDIS_USER=exporter`, `REDIS_PASSWORD=${REDIS_EXPORTER_PASSWORD}`, `REDIS_EXPORTER_CHECK_SINGLE_STREAMS=stream:payout.requested,stream:payout.requested.dlq` (the exporter's own variable names, not app config) |
| grafana | `GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_ADMIN_PASSWORD}`, `GF_AUTH_ANONYMOUS_ENABLED=false`, `GF_USERS_ALLOW_SIGN_UP=false`, `GF_ANALYTICS_REPORTING_ENABLED=false`; provisioning and dashboards mounted read-only |
| prometheus | `command: --config.file=/etc/prometheus/prometheus.yml --storage.tsdb.retention.time=30d --enable-feature=exemplar-storage --web.enable-remote-write-receiver=false` |

### 7.4 `compose.test.yaml` (override used by `stack.sh up` with the test profile)

```yaml
x-limits-raised: &limits-raised
  SERVICE_RATE_LIMIT_USER: "1000000"
  SERVICE_RATE_LIMIT_WORKER: "1000000"

x-via-toxiproxy: &via-toxiproxy
  DB_HOST: toxiproxy
  DB_PORT: "15432"
  REDIS_HOST: toxiproxy
  REDIS_PORT: "16379"

services:
  identity:
    environment:
      <<: [*limits-raised, *via-toxiproxy]
      AUTH_LOGIN_LIMIT_ACCOUNT: "1000000"
      AUTH_LOGIN_LIMIT_IP: "1000000"
    ports: ["127.0.0.1:${IDENTITY_PORT}:8000"]
    depends_on: { toxiproxy: { condition: service_healthy } }
  payroll:
    environment: { <<: [*limits-raised, *via-toxiproxy] }
    ports: ["127.0.0.1:${PAYROLL_PORT}:8000"]
    depends_on: { toxiproxy: { condition: service_healthy } }
  payout-worker:
    environment:
      <<: *via-toxiproxy
      PAYOUT_PAYROLL_BASE_URL: http://toxiproxy:18001   # C46
      BANK_BASE_URL: http://toxiproxy:18002
    ports: ["127.0.0.1:${PAYOUT_WORKER_PORT}:8000"]
    depends_on: { toxiproxy: { condition: service_healthy } }
  bank-stub:
    environment: { DB_HOST: toxiproxy, DB_PORT: "15432" }
    ports: ["127.0.0.1:${BANK_STUB_PORT}:8000"]
    depends_on: { toxiproxy: { condition: service_healthy } }
  web:
    environment:
      PAYROLL_BASE_URL: http://toxiproxy:18001
      REDIS_HOST: toxiproxy                          # C1/C12 names; no REDIS_URL
      REDIS_PORT: "16379"
      BFF_RATE_LIMIT_SESSION: "1000000"
      BFF_RATE_LIMIT_IP: "1000000"
    depends_on: { toxiproxy: { condition: service_healthy } }
  toxiproxy:
    image: ghcr.io/shopify/toxiproxy
    profiles: [test]
    command: ["-host=0.0.0.0", "-config=/config/toxiproxy.json"]
    volumes: ["./toxiproxy.json:/config/toxiproxy.json:ro"]
    ports: ["127.0.0.1:${TOXIPROXY_PORT}:8474"]
    healthcheck: { test: ["CMD", "/toxiproxy-cli", "list"], interval: 3s, retries: 10 }
    depends_on: { postgres: { condition: service_healthy }, redis: { condition: service_healthy } }
    networks: [payrun]
  tempo:        { ports: ["127.0.0.1:${TEMPO_PORT}:3200"] }
  loki:         { ports: ["127.0.0.1:${LOKI_PORT}:3100"] }
  prometheus:   { ports: ["127.0.0.1:${PROMETHEUS_PORT}:9090"] }
  alertmanager: { ports: ["127.0.0.1:${ALERTMANAGER_PORT}:9093"] }
  alert-sink:   { ports: ["127.0.0.1:${ALERT_SINK_PORT}:8080"] }
```

- Migrations, the exporters and `bootstrap-owner` keep direct connections, so a toxic never breaks start-up or monitoring.
- Web → identity stays direct; the "identity down at login" smoke test stops the `identity` container instead (plan §12.1 row "identity down").
- An integration test in identity and web proves the production limits still lock out (plan §2.2); the raised values exist only in this file.

**`toxiproxy.json`:**

```json
[
  {"name": "postgres",  "listen": "0.0.0.0:15432", "upstream": "postgres:5432",  "enabled": true},
  {"name": "redis",     "listen": "0.0.0.0:16379", "upstream": "redis:6379",     "enabled": true},
  {"name": "payroll",   "listen": "0.0.0.0:18001", "upstream": "payroll:8000",   "enabled": true},
  {"name": "bank-stub", "listen": "0.0.0.0:18002", "upstream": "bank-stub:8000", "enabled": true}
]
```

`toxiproxy` depends on postgres and redis only; the `payroll` and `bank-stub` proxies resolve their
upstreams lazily per connection, so there is no start-up cycle with services that depend on it.
`tests/resilience` reset all toxics in teardown through the API on `TOXIPROXY_PORT`.

## 8. Observability configs (`deploy/observability/`; PAY-NFR-04, 09, 10, 11)

Component names follow the pinned versions; the late-2026 names are used (`otlp_grpc`,
`otlp_http`, `resource_constant_labels`). The scaffold task checks them against the pinned
Collector with `otelcol-contrib validate --config`, which `manifests.sh` also runs.

### 8.1 Collector (`collector.yaml`)

```yaml
extensions:
  health_check: { endpoint: 0.0.0.0:13133 }

receivers:
  otlp:
    protocols:
      grpc: { endpoint: 0.0.0.0:4317 }
      http: { endpoint: 0.0.0.0:4318, max_request_body_size: 4194304 }

processors:
  memory_limiter: { check_interval: 1s, limit_percentage: 80, spike_limit_percentage: 25 }
  batch: { send_batch_size: 1024, timeout: 2s }
  # defence in depth for PAY-NFR-07: drop deny-listed attributes, mask token-looking query strings
  transform/redact:
    error_mode: ignore
    trace_statements:
      - context: span
        statements:
          - delete_matching_keys(attributes, "(?i).*(password|token|authorization|cookie|account|routing|api_key|secret|salary|amount).*")
          - replace_pattern(attributes["url.full"], "([?&#](token|code|key)=)[^&#]*", "$$1[REDACTED]")
          - delete_key(attributes, "url.query")
    log_statements:
      - context: log
        statements:
          - delete_matching_keys(attributes, "(?i).*(password|token|authorization|cookie|account|routing|api_key|secret|salary|amount).*")
          - replace_pattern(body, "eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+", "[REDACTED]")
  resource/env:
    attributes:
      - { key: deployment.environment.name, value: dev, action: insert }   # only if the SDK didn't set it

exporters:
  otlp_grpc/tempo: { endpoint: tempo:4317, tls: { insecure: true } }
  prometheus:
    endpoint: 0.0.0.0:8889
    enable_open_metrics: true                        # exemplars
    resource_constant_labels:
      included: ["service.name", "deployment.environment.name"]
    metric_expiration: 2m                            # C72: a restarted process's stale series drop fast
  otlp_http/loki: { endpoint: http://loki:3100/otlp }

service:
  extensions: [health_check]
  telemetry:
    metrics:
      readers:
        - pull: { exporter: { prometheus: { host: 0.0.0.0, port: 8888 } } }
  pipelines:
    traces:  { receivers: [otlp], processors: [memory_limiter, resource/env, transform/redact, batch], exporters: [otlp_grpc/tempo] }
    metrics: { receivers: [otlp], processors: [memory_limiter, resource/env, batch], exporters: [prometheus] }
    logs:    { receivers: [otlp], processors: [memory_limiter, resource/env, transform/redact, batch], exporters: [otlp_http/loki] }
```

**`collector.prod.yaml`** (used by the Kubernetes prod overlay; uat uses `collector.yaml` with backend
endpoints patched). The apps send 100% of spans (`parentbased_always_on`, C23), and prod sampling
happens here (R27): the same file as above, plus `tail_sampling` before `batch` in the traces pipeline,
and exporters pointing at the hosted or Helm-installed backends (`${env:TRACES_ENDPOINT}`, …):

```yaml
processors:
  tail_sampling:
    decision_wait: 10s
    num_traces: 50000
    expected_new_traces_per_sec: 100
    policies:
      - { name: errors,   type: status_code,   status_code: { status_codes: [ERROR] } }      # keep every error
      - { name: slow,     type: latency,       latency: { threshold_ms: 300 } }               # keep traces > 300 ms
      - { name: baseline, type: probabilistic, probabilistic: { sampling_percentage: 10 } }   # 10% of the rest
service:
  pipelines:
    traces: { receivers: [otlp], processors: [memory_limiter, resource/env, transform/redact, tail_sampling, batch], exporters: [otlp_grpc/tempo] }
```

A trace is kept when any policy matches. The Collector runs as a single-replica gateway Deployment so
every span of a trace reaches the same tail sampler: every overlay sets `otel-collector` to 1 replica,
and `test_k8s_policy.py` asserts it. `x-trace-id` is still returned for every request;
in prod a dropped trace's ID simply finds nothing in Tempo.

### 8.2 `prometheus/prometheus.yml`

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s
  external_labels: { env: dev }

rule_files: ["/etc/prometheus/rules/*.yml"]

alerting:
  alertmanagers:
    - static_configs: [{ targets: ["alertmanager:9093"] }]

scrape_configs:
  - job_name: otel-collector              # application metrics (OTLP → prometheus exporter)
    honor_labels: true                    # keep job/instance/service_name set by the exporter
    static_configs: [{ targets: ["otel-collector:8889"] }]
  - job_name: otelcol                     # the Collector's own telemetry (CollectorDropping)
    static_configs: [{ targets: ["otel-collector:8888"] }]
  - job_name: postgres
    static_configs: [{ targets: ["postgres-exporter:9187"] }]
  - job_name: redis
    static_configs: [{ targets: ["redis-exporter:9121"] }]
  - job_name: prometheus
    static_configs: [{ targets: ["localhost:9090"] }]
  - job_name: alertmanager
    static_configs: [{ targets: ["alertmanager:9093"] }]
  - job_name: tempo
    static_configs: [{ targets: ["tempo:3200"] }]
  - job_name: loki
    static_configs: [{ targets: ["loki:3100"] }]
```

Retention 30 days (flag in §7.3); exemplar storage on.

### 8.3 Rules (`prometheus/rules/`)

**SLI route selector (C4).** BFF availability counts the J1–J5 routes, i.e. every BFF route except the
health routes and telemetry: `route!~"/api/livez|/api/readyz|/api/telemetry"`. `route` label values are
the web LLD's route templates and `status` the numeric code (web LLD §7.2).
"Heavy" routes are calculate and approve (p95 < 1.5 s); the rest are reads and simple writes (p95 < 300 ms).

**`recording.yml`:**

```yaml
groups:
  - name: slo-bff-availability
    interval: 30s
    rules:
      - record: sli:bff_errors:ratio_rate5m
        expr: |
          sum(rate(bff_request_duration_seconds_count{route!~"/api/livez|/api/readyz|/api/telemetry", status=~"5.."}[5m]))
          /
          sum(rate(bff_request_duration_seconds_count{route!~"/api/livez|/api/readyz|/api/telemetry"}[5m]))
      - record: sli:bff_errors:ratio_rate30m
        expr: |
          sum(rate(bff_request_duration_seconds_count{route!~"/api/livez|/api/readyz|/api/telemetry", status=~"5.."}[30m]))
          /
          sum(rate(bff_request_duration_seconds_count{route!~"/api/livez|/api/readyz|/api/telemetry"}[30m]))
      - record: sli:bff_errors:ratio_rate1h
        expr: |
          sum(rate(bff_request_duration_seconds_count{route!~"/api/livez|/api/readyz|/api/telemetry", status=~"5.."}[1h]))
          /
          sum(rate(bff_request_duration_seconds_count{route!~"/api/livez|/api/readyz|/api/telemetry"}[1h]))
      - record: sli:bff_errors:ratio_rate6h
        expr: |
          sum(rate(bff_request_duration_seconds_count{route!~"/api/livez|/api/readyz|/api/telemetry", status=~"5.."}[6h]))
          /
          sum(rate(bff_request_duration_seconds_count{route!~"/api/livez|/api/readyz|/api/telemetry"}[6h]))
  - name: slo-bff-latency
    interval: 30s
    rules:
      - record: sli:bff_latency_p95:5m
        expr: |
          histogram_quantile(0.95, sum by (le) (rate(bff_request_duration_seconds_bucket{route!~"/api/livez|/api/readyz|/api/telemetry|.*/calculate|.*/approve"}[5m])))
      - record: sli:bff_latency_heavy_p95:5m
        expr: |
          histogram_quantile(0.95, sum by (le) (rate(bff_request_duration_seconds_bucket{route=~".*/calculate|.*/approve"}[5m])))
  - name: slo-settlement
    interval: 1m
    rules:
      - record: sli:payout_settlement_p95:1h
        expr: histogram_quantile(0.95, sum by (le) (rate(payroll_payout_settlement_seconds_bucket[1h])))
  - name: dependencies
    interval: 30s
    rules:
      - record: provider:external_requests:rate5m
        expr: sum by (provider, outcome) (rate(external_requests_total[5m]))
      - record: provider:external_errors:ratio_rate5m
        expr: |
          sum by (provider) (rate(external_requests_total{outcome!="ok"}[5m]))
          /
          sum by (provider) (rate(external_requests_total[5m]))
```

`ratio_rate30m` is the HLD's `sli:bff_errors:ratio_rate{5m,30m,1h,6h}` set. A window with no traffic
gives NaN, which compares false, so the burn alerts stay quiet when idle.

**`alerts.yml`** (runbook base `https://github.com/Positron00/FinApp/blob/main/docs/runbooks/`, the public repo, so links work from any receiver). Expressions taken from plan §11 are verbatim; where the plan gives only a description, the expression and `for` are this design's and are marked †.

| Alert | Expr | for | severity | Extra labels |
|---|---|---|---|---|
| `BffAvailabilityFastBurn` | `sli:bff_errors:ratio_rate1h > (14.4 * 0.005) and sli:bff_errors:ratio_rate5m > (14.4 * 0.005)` | 2m | page | `service_name: web`, `slo: bff-availability` |
| `BffAvailabilitySlowBurn` | `sli:bff_errors:ratio_rate6h > (6 * 0.005) and sli:bff_errors:ratio_rate30m > (6 * 0.005)` | 15m † | ticket | `service_name: web`, `slo: bff-availability` |
| `PayoutSettlementSlow` | `max(payroll_runs_processing_oldest_age_seconds) > 60` | 2m | page | `service_name: payroll` |
| `PayoutDLQ` | `increase(queue_messages_total{outcome="dlq"}[15m]) > 0 or (queue_messages_total{outcome="dlq"} > 0 unless max_over_time(queue_messages_total{outcome="dlq"}[1h] offset 15m))` (C75) | 0m | page | `service_name: payout-worker` |
| `PayoutNeedsOperator` | `max(payroll_attempts_needs_operator) > 0` | 1m † | page | `service_name: payroll` |
| `PayoutLateSettlement` | `increase(payroll_late_settlements_total[15m]) > 0 or (payroll_late_settlements_total > 0 unless max_over_time(payroll_late_settlements_total[1h] offset 15m))` (C75) | 0m | page | `service_name: payroll`; runbook `docs/runbooks/PayoutLateSettlement.md` (possible double payment) |
| `PayoutsStuck` | `max(payroll_payouts_stuck) > 0` | 10m | page | `service_name: payroll` |
| `PayoutStreamNearMaxlen` † | `max(redis_stream_length{stream="stream:payout.requested"}) > 0.8 * 100000` | 5m | warning | `service_name: redis` |
| `EmailOutboxStuck` † | `max(identity_email_oldest_pending_age_seconds) > 900 or sum(increase(identity_emails_failed_total[1h])) > 0 or sum(identity_emails_failed_total > 0 unless max_over_time(identity_emails_failed_total[2h] offset 1h)) > 0` (R25, C20, C75) | 5m | warning | `service_name: identity` |
| `RateLimiterErrors` † | `sum by (service_name, component) (rate(rate_limiter_errors_total[5m])) > 0` | 5m | warning | — |
| `OutboxStuck` † | `max by (service_name) (outbox_oldest_age_seconds) > 60` | 1m | warning | — |
| `ConsumerLagGrowing` † | `max by (stream, group) (queue_consumer_lag) > 0 and max by (stream, group) (deriv(queue_consumer_lag[10m])) > 0` | 10m | warning | `service_name: payout-worker` |
| `BankBreakerOpen` † | `max(circuit_breaker_state{target="bank"}) == 2` | 5m | warning | `service_name: payout-worker` |
| `BankErrorRateHigh` † | `provider:external_errors:ratio_rate5m{provider="bank"} > 0.05` | 10m | warning | `service_name: payout-worker` |
| `SigninFailuresSpike` † | `sum(increase(auth_signins_total{outcome="failure"}[15m])) > 50` (C19) | 0m | warning | `service_name: identity` |
| `RedisMemoryHigh` † | `max(redis_memory_used_bytes / (redis_memory_max_bytes > 0)) > 0.8` | 5m | warning | `service_name: redis` |
| `PodRestarts` † | `increase(kube_pod_container_status_restarts_total{namespace=~"payrun-.*"}[15m]) > 2` | 0m | warning | — (Kubernetes only, R28) |
| `CollectorDropping` † | `(sum(rate({__name__=~"otelcol_exporter_send_failed_spans(_total)?"}[5m])) or vector(0)) + (sum(rate({__name__=~"otelcol_exporter_send_failed_metric_points(_total)?"}[5m])) or vector(0)) + (sum(rate({__name__=~"otelcol_exporter_send_failed_log_records(_total)?"}[5m])) or vector(0)) + (sum(rate({__name__=~"otelcol_receiver_refused_spans(_total)?"}[5m])) or vector(0)) > 0` (Collector 0.164 exports its own counters without `_total`; the regex takes either name) | 10m | warning | `service_name: otel-collector` |

Notes:
- **BankErrorRateHigh excludes business rejections by construction:** a bank `rejected` answer is HTTP 2xx, so the resilient client counts it as `outcome="ok"`; business outcomes are counted separately in `payouts_bank_transfers_total{outcome, reason}`.
- **PayoutDLQ:** the worker pre-initialises `queue_messages_total{outcome="dlq"}` to 0 (C21; payout-worker LLD §3.3). That alone isn't enough: a DLQ inside a restarted worker's first export interval reaches Prometheus as a series that starts at 1, which `increase()` reads as 0. The live drill hit exactly this (C75). So the rule also fires on a series above 0 that had **no sample in the hour before the window** (`unless max_over_time(X[1h] offset 15m)`); PayoutLateSettlement uses the same arm, and EmailOutboxStuck `unless max_over_time(X[2h] offset 1h)` for its 1 h window. The first version compared with one instant (`unless X offset 15m`), which also fired for an **old** series above 0 whenever that instant fell more than the 5 m lookback into a data gap (a Prometheus restart, a Collector outage, or `metric_expiration` dropping a stalled exporter). Set operators match on every label except `__name__`, so `unless` matches `max_over_time`'s output without `ignoring`. promtool cases cover a new series (fires, clears after the window) and an old series behind a gap (quiet) for all three alerts; the Queues panels and `alert_drills.sh` use the same arm.
- The `max()` in the payroll gauge alerts collapses replicas, as the plan requires.
- **EmailOutboxStuck** uses the counter, not a gauge of failed rows, so it clears one hour after the last failure once SMTP is fixed (R25); a new failure series clears an hour after it first appeared.
- **PodRestarts** needs kube-state-metrics, a listed cluster dependency (R28). The rule ships in the Kubernetes observability install only, not in the Compose Prometheus; its promtool test still runs in CI.
- Every alert has these annotations:

```yaml
annotations:
  summary: "<one line, e.g. 'Payouts were dead-lettered in the last 15 minutes'>"
  description: "<what it means and the first check, with {{ $value }} where useful>"
  runbook_url: "https://github.com/Positron00/FinApp/blob/main/docs/runbooks/<AlertName>.md"
  dashboard_url: "http://127.0.0.1:4101/d/<dashboard uid>"     # the dev Grafana (Compose)
```

Example, verbatim in the file:

```yaml
- alert: PayoutDLQ
  expr: 'increase(queue_messages_total{outcome="dlq"}[15m]) > 0 or (queue_messages_total{outcome="dlq"} > 0 unless max_over_time(queue_messages_total{outcome="dlq"}[1h] offset 15m))'
  labels: { severity: page, service_name: payout-worker }
  annotations:
    summary: "Payout messages were dead-lettered in the last 15 minutes"
    description: "{{ $value }} message(s) moved to {{ $labels.stream }}.dlq. Money may be stuck: follow the DLQ replay steps."
    runbook_url: "https://github.com/Positron00/FinApp/blob/main/docs/runbooks/PayoutDLQ.md"
    dashboard_url: "http://127.0.0.1:4101/d/payrun-queues"
```

**Alert → dashboard:** BFF burn alerts → `payrun-journeys`; Payout*, Outbox, ConsumerLag, EmailOutboxStuck ("Identity emails" panel) → `payrun-queues`; Bank* → `payrun-bank`; RateLimiterErrors, SigninFailuresSpike → `payrun-service-overview`; Redis, Pod, Collector → `payrun-platform`.

**`dashboard_url` outside dev:** the URLs in `alerts.yml` and `alerts-kubernetes.yml` are hard-coded to the dev Grafana (`http://127.0.0.1:4101`); nothing rewrites them per environment. A uat or prod install must template the host itself, for example from an external label (`global.external_labels` with the Grafana base URL, used as `{{ $externalLabels.grafana_url }}/d/<uid>` in the annotation) or in the Alertmanager notification template. Until then the link in a uat or prod notification points at a dev address; the runbook link does not depend on it.

**promtool tests** (`prometheus/tests/slo_test.yml` loads `recording.yml`; `alerts_test.yml` loads both files; `evaluation_interval: 30s`). Every alert has at least one **fires** and one **stays quiet** case (`test_rules_meta.py` enforces this):

| Alert | Fires when (input series) | Stays quiet when |
|---|---|---|
| `BffAvailabilityFastBurn` | 10% of `bff_request_duration_seconds_count` are `status="500"` for 70 m (1 h ratio 0.1 > 0.072); expected at 65m with labels and the full annotations | 5% errors (< 0.072), and a 4 m spike of 100% errors (5 m window high, 1 h low); only `/api/readyz` and `/api/livez` failing (C4) |
| `BffAvailabilitySlowBurn` | 4% errors for 7 h (> 0.03) | 2% errors for 7 h; no traffic (NaN) |
| `PayoutSettlementSlow` | gauge 0 then 90 for 5 m | 45 for 30 m; 90 for 1 m only |
| `PayoutDLQ` | counter `0 0 0 1 1 1 …` (pre-initialised); a series that first appears at 1 (C75), which clears after 15 m | flat 0; retries only; an old series at 3 with a 10 m data gap, at 52 m and 55 m (C75) |
| `PayoutNeedsOperator` | 1 for 3 m (one replica 0, another 1, so `max` matters) | 0 |
| `PayoutLateSettlement` | counter `0 0 0 1 …` (pre-initialised); a series that first appears at 1 (a restarted payroll; C75), which clears after 15 m | flat, then clear 15 m after the increment; an old series at 3 with a 10 m data gap, at 52 m and 55 m (C75) |
| `PayoutsStuck` | 2 for 12 m | 2 for 8 m, then 0 |
| `PayoutStreamNearMaxlen` | 85000 for 10 m | 79000 |
| `EmailOutboxStuck` | oldest pending 1000 s for 6 m; separately `identity_emails_failed_total` going 0 → 1 (fires after 5 m); a failure series that first appears at 1 (`_ _ 1x80`: pending from 2 m, firing at 7 m, quiet at 125 m; C75) | pending 300 s and the counter flat; **clears** 1 h after the counter stops rising (R25); an old failure series at 3 with a 20 m data gap, at 105 m and 110 m (C75; 20 m, so the old instant arm would have outlasted `for: 5m`) |
| `RateLimiterErrors` | counter rising for 10 m | flat |
| `OutboxStuck` | oldest age 120 for 3 m | 20 |
| `ConsumerLagGrowing` | lag rising 0 → 500 linearly over 25 m | a 500-message burst draining within 2 m |
| `BankBreakerOpen` | 2 for 6 m | 2 for 3 m then 0; 1 (half-open) |
| `BankErrorRateHigh` | 10% `timeout` outcomes for 15 m | 4%; 100% `ok` (including business rejections) |
| `SigninFailuresSpike` | 60 `outcome="failure"` in 15 m | 30 failures; 1000 `success`; 100 `rate_limited` (C19) |
| `RedisMemoryHigh` | used 0.9 × max for 6 m | 0.5 × max; `max = 0` (no limit) |
| `PodRestarts` | +3 restarts in 15 m | +1 |
| `CollectorDropping` | failed spans rising for 12 m | series absent (`or vector(0)`); every failure counter present and flat, once with the unsuffixed names and once with `_total` |

SLO recording tests check `sli:bff_errors:ratio_rate5m` = 0.1 for a 10% input, the route exclusion (5xx on `/api/livez`, `/api/readyz` and `/api/telemetry` don't count), and `sli:payout_settlement_p95:1h` against hand-computed bucket input (p95 within 45–60 s for a run settling in about 50 s).

### 8.4 `alertmanager.yml` (ADR 0010)

```yaml
global:
  resolve_timeout: 5m

route:
  receiver: ticket
  group_by: [alertname, service_name]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - matchers: ['severity="page"']
      receiver: page
      group_wait: 10s
      repeat_interval: 1h
    - matchers: ['severity=~"ticket|warning"']
      receiver: ticket

inhibit_rules:
  - source_matchers: ['alertname="BffAvailabilityFastBurn"']
    target_matchers: ['alertname="BffAvailabilitySlowBurn"']

receivers:
  - name: page
    webhook_configs:
      - url_file: /etc/alertmanager/secrets/page_url
        send_resolved: true
  - name: ticket
    webhook_configs:
      - url_file: /etc/alertmanager/secrets/ticket_url
        send_resolved: true
```

The same file works in every environment: Compose writes `page_url` and `ticket_url` from
`ALERT_PAGE_WEBHOOK_URL` / `ALERT_TICKET_WEBHOOK_URL` into a tmpfs (§7.2); Kubernetes mounts them
from a Secret (ExternalSecret in uat/prod). Dev and CI point both at alert-sink (`/page`, `/ticket`);
real receivers are the user's choice (ADR 0010). Paging alerts are never inhibited by another alert,
so a drill always sees each one. CI runs `amtool check-config`.

### 8.5 alert-sink (D5)

`deploy/observability/alert-sink/alert_sink.py`: Python stdlib only (`http.server.ThreadingHTTPServer`),
run with `python /app/alert_sink.py` in the pinned Python base image as uid 65534, read-only rootfs.

| Method | Path | Behaviour |
|---|---|---|
| POST | `/page`, `/ticket` | Store the Alertmanager JSON payload (≤ 256 KB) with a receive timestamp in a per-route ring buffer of 500; log one JSON line per alert (`alert_sink.received` {route, alertname, status}); 200 |
| GET | `/page`, `/ticket` | 200 with the stored payloads, oldest first |
| DELETE | `/all` | Clear both buffers (drill setup); 204 |
| GET | `/healthz` | 200 |

Chosen over a third-party webhook-tester image: nothing new to pin, scan or trust, and the drills
need exactly "list what arrived per route". Alert drills poll `GET /page` on `ALERT_SINK_PORT`.

### 8.6 Tempo (`tempo.yaml`) and Loki (`loki.yaml`)

```yaml
# tempo.yaml (single binary, local storage; traces 7 days)
server: { http_listen_port: 3200 }
distributor:
  receivers:
    otlp: { protocols: { grpc: { endpoint: 0.0.0.0:4317 } } }
ingester: { max_block_duration: 5m }
compactor:
  compaction: { block_retention: 168h }
storage:
  trace:
    backend: local
    wal: { path: /var/tempo/wal }
    local: { path: /var/tempo/blocks }
```

```yaml
# loki.yaml (Loki 3.x, single binary, OTLP ingest; logs 14 days)
auth_enabled: false
server: { http_listen_port: 3100 }
common:
  path_prefix: /loki
  replication_factor: 1
  ring: { kvstore: { store: inmemory } }
  storage: { filesystem: { chunks_directory: /loki/chunks, rules_directory: /loki/rules } }
schema_config:
  configs:
    - { from: "2026-01-01", store: tsdb, object_store: filesystem, schema: v13, index: { prefix: index_, period: 24h } }
limits_config:
  retention_period: 336h
  allow_structured_metadata: true
  otlp_config:
    resource_attributes:
      attributes_config:
        - action: index_label
          attributes: [service.name, deployment.environment.name]
compactor:
  working_directory: /loki/compactor
  retention_enabled: true
  delete_request_store: filesystem
```

OTLP log records keep `trace_id`, `span_id` and `severity_text` as structured metadata, so the
trace test's query `{service_name=~".+"} | trace_id="<id>"` needs no parser. Retention: traces 7 d,
logs 14 d, metrics 30 d (plan §11).

### 8.7 Grafana provisioning

`grafana/provisioning/datasources/datasources.yaml`:

```yaml
apiVersion: 1
datasources:
  - name: Prometheus
    uid: prometheus
    type: prometheus
    url: http://prometheus:9090
    isDefault: true
    jsonData:
      exemplarTraceIdDestinations:
        - { name: trace_id, datasourceUid: tempo }
  - name: Tempo
    uid: tempo
    type: tempo
    url: http://tempo:3200
    jsonData:
      tracesToLogsV2:
        datasourceUid: loki
        spanStartTimeShift: "-5m"
        spanEndTimeShift: "5m"
        customQuery: true
        query: '{service_name="${__span.tags["service.name"]}"} | trace_id="${__trace.traceId}"'
      tracesToMetrics:
        datasourceUid: prometheus
  - name: Loki
    uid: loki
    type: loki
    url: http://loki:3100
    jsonData:
      derivedFields:
        - name: trace_id
          matcherType: label            # trace_id is structured metadata, not in the line
          matcherRegex: trace_id
          datasourceUid: tempo
          url: "$${__value.raw}"
```

`grafana/provisioning/dashboards/dashboards.yaml`: one file provider `PayRun`, folder `PayRun`,
path `/var/lib/grafana/dashboards`, `allowUiUpdates: false`, `disableDeletion: true`.

### 8.8 Dashboards (`grafana/dashboards/*.json`)

All use datasource uids `prometheus`, `tempo`, `loki`; default range 6 h; refresh 30 s; histogram
panels show exemplars; per-service metrics are filtered by `service_name`.

**`service-overview.json`** (uid `payrun-service-overview`; variable `service` from `label_values(http_server_request_duration_seconds_count, service_name)` plus `web`):

| Panel | Query (summary) |
|---|---|
| Request rate by route | `sum by (http_route) (rate(http_server_request_duration_seconds_count{service_name="$service"}[5m]))` (web: `bff_request_duration_seconds_count` by `route`) |
| 5xx ratio by route | 5xx count / all count, by route |
| Latency p50 / p95 / p99 by route | `histogram_quantile` over the `_bucket` series, exemplars on |
| DB pool in use | `db_client_connections_usage{service_name="$service"}` by state |
| Event-loop lag | `asyncio_event_loop_lag_seconds` |
| CPU and memory | process metrics from the system-metrics instrumentation |
| Dependency calls | `rate(external_requests_total{service_name="$service"}[5m])` by provider and outcome |
| Breaker state | `circuit_breaker_state{service_name="$service"}` (state timeline) |
| Rate limiting | `rate_limited_total` by scope; `rate_limiter_errors_total` by component |
| Sign-ins (identity) | `auth_signins_total` by outcome (`success`, `failure`, `rate_limited`) |
| Warnings and errors | Loki `{service_name="$service"} \| severity_text=~"(?i)warn.*\|error"` |
| Error traces | Tempo TraceQL `{resource.service.name="$service" && status=error}` |

**`journeys.json`** (uid `payrun-journeys`): an SLO row, then one row per journey.

| Row | Panels |
|---|---|
| SLO | Availability over 30 d (`1 - avg_over_time(sli:bff_errors:ratio_rate1h[30d])`) vs 99.5%; error budget remaining; burn rate 1 h and 6 h (ratio ÷ 0.005) with 14.4× and 6× lines; `sli:bff_latency_p95:5m` vs 300 ms; `sli:bff_latency_heavy_p95:5m` vs 1.5 s; `sli:payout_settlement_p95:1h` vs 60 s |
| J1 Access | success ratio and p95 for the login, accept and invite BFF routes; `auth_signins_total` by outcome |
| J2 Company setup | employees and company-settings routes: success and p95 |
| J3 Prepare a pay run | create, hours and calculate routes: success and p95; `payroll_pay_runs_total{outcome="calculated"}` |
| J4 Approve and pay | approve route success and p95; `payroll_pay_runs_total` by outcome; `payroll_payouts_total` by outcome; `payroll_runs_processing_oldest_age_seconds`; `payroll_payout_settlement_seconds` heatmap |
| J5 Recover | retry route success; `payroll_payouts_total{outcome="failed"}` |

**`queues.json`** (uid `payrun-queues`): `outbox_unsent` and `outbox_oldest_age_seconds` by service (60 s line); `queue_messages_total` rate by outcome; `queue_processing_seconds` p50 / p95; `queue_consumer_lag`; `queue_dlq_messages`; `redis_stream_length` for both streams (80000 line); `payroll_payouts_republished_total` rate; `payroll_payouts_stuck`; `payroll_attempts_needs_operator`; `payroll_runs_processing_oldest_age_seconds` (60 s line); `identity_emails_pending`, `increase(identity_emails_failed_total[1h])` (C20), `identity_email_oldest_pending_age_seconds`; a log panel for `queue.*`, `outbox.*` and `payout.*` events.

**`bank.json`** (uid `payrun-bank`; the "Bank (stub) dependency" dashboard): `external_requests_total{provider="bank"}` by outcome; error ratio vs 5%; `external_request_duration_seconds{provider="bank"}` p50 / p95 / p99 (exemplars); `circuit_breaker_state{target="bank"}`; `payouts_bank_transfers_total` by outcome and reason; the same request, latency and breaker panels for `provider="payroll"` (worker → payroll); a log panel for `bank.transfer.*` events.

**`platform.json`** (uid `payrun-platform`): Postgres `pg_up`, connections by role, transactions per second, deadlocks, database size; Redis `redis_up`, memory used vs max (80% line), connected clients, commands per second, AOF status, `redis_evicted_keys_total` (must stay 0); Collector accepted, refused and failed spans, metric points and log records; pod restarts and container CPU / memory (Kubernetes only; empty in Compose); scrape-target `up` table; firing alerts (`ALERTS{alertstate="firing"}`).

## 9. Kubernetes (`deploy/k8s/`; PAY-NFR-13, ADR 0008, 0011)

### 9.1 Layout

```text
deploy/k8s/
  base/
    kustomization.yaml            # resources below; commonLabels app.kubernetes.io/part-of: payrun
    common/
      networkpolicy-default-deny.yaml   networkpolicy-allow-dns.yaml
      configmap-payout-timing.yaml      # the x-payout-timing keys, shared by payroll and payout-worker
    web/  identity/  payroll/  payout-worker/
      deployment.yaml  service.yaml (not payout-worker)  serviceaccount.yaml  configmap.yaml
      secret.yaml (placeholders)  pdb.yaml  networkpolicy.yaml
    otel-collector/  deployment.yaml service.yaml networkpolicy.yaml
      kustomization.yaml          # configMapGenerator otel-collector-config from collector.yaml
      collector.yaml              # synced copy (sync-k8s-assets.sh; R32)
    bootstrap/
      kustomization.yaml          # configMapGenerator db-init from init/ (no load-restrictor flag; C36)
      init/                       # synced copy of deploy/compose/init/ (R32); never edited by hand
      job-db-bootstrap.yaml  job-bootstrap-owner.yaml  networkpolicy.yaml
    migrations/
      kustomization.yaml          # nameSuffix: -0-1-0  (bumped by the release step)
      job-migrate-identity.yaml  job-migrate-payroll.yaml  job-migrate-payouts.yaml  networkpolicy.yaml
  components/                     # kind: Component
    bank-stub/                    # Deployment, Service, SA, ConfigMap, Secret, PDB, NetworkPolicies,
                                  # migrate-bank-stub Job, worker→bank-stub egress allow
    in-cluster-data/              # dev: postgres and redis StatefulSets (+ redis ACL entrypoint ConfigMap), allows
      redis/                      # synced copy of redis.conf and entrypoint.sh (R32)
    mailpit/                      # dev, uat: in-cluster SMTP capture + identity→mailpit:1025 allow
    managed-data-egress/          # uat, prod: ipBlock egress to the managed Postgres (5432) and Redis (6379) CIDRs
    bank-egress-ipblock/          # prod: payout-worker → 0.0.0.0/0 minus private ranges, port 443
    smtp-egress/                  # prod: identity → provider CIDR, port 587
    external-secrets/             # uat, prod: ExternalSecret per Secret; deletes the placeholder Secrets
    ingress/                      # uat, prod: Ingress for web only, TLS
  overlays/dev|uat|prod/kustomization.yaml (+ patches/)
  overlays/prod/collector.prod.yaml   # synced copy; replaces the Collector ConfigMap in prod
  schemas/                        # vendored CRD JSON schemas for kubeconform (§9.6)
```

### 9.2 Base resources per service

**Deployment** (identity shown; others differ in name, image, env and replicas):

```yaml
spec:
  replicas: 1                                  # overlays set it
  revisionHistoryLimit: 5
  strategy: { type: RollingUpdate, rollingUpdate: { maxUnavailable: 0, maxSurge: 1 } }
  template:
    metadata:
      labels: { app.kubernetes.io/name: identity, app.kubernetes.io/part-of: payrun }
    spec:
      serviceAccountName: identity
      automountServiceAccountToken: false
      terminationGracePeriodSeconds: 30        # payout-worker: 75 (plan §2.2)
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        fsGroup: 10001
        seccompProfile: { type: RuntimeDefault }
      containers:
        - name: identity
          image: payrun/identity:0.1.0           # overlays set registry and tag/digest
          ports: [{ name: http, containerPort: 8000 }]
          envFrom:
            - configMapRef: { name: identity-config }
            - configMapRef: { name: payout-timing }       # payroll and payout-worker only
            - secretRef: { name: identity-secrets }
          env:
            - { name: OTEL_SEMCONV_STABILITY_OPT_IN, value: http }
            - { name: OTEL_EXPORTER_OTLP_ENDPOINT, value: http://otel-collector:4317 }
            - { name: OTEL_TRACES_SAMPLER, value: parentbased_always_on }   # C23, every overlay
            - { name: OTEL_EXPORTER_OTLP_TIMEOUT, value: "3" }   # seconds; Python Deployments only (C67)
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: { drop: [ALL] }
          startupProbe:   { httpGet: { path: /livez, port: http }, periodSeconds: 2, failureThreshold: 15 }   # 30 s
          livenessProbe:  { httpGet: { path: /livez, port: http }, periodSeconds: 10, timeoutSeconds: 2, failureThreshold: 3 }
          readinessProbe: { httpGet: { path: /readyz, port: http }, periodSeconds: 5, timeoutSeconds: 2, failureThreshold: 3 }
          resources:
            requests: { cpu: 100m, memory: 192Mi }
            limits:   { memory: 512Mi }            # identity: ≥ 512Mi for argon2 (identity LLD §4.2); others 384Mi; no CPU limit
          volumeMounts: [{ name: tmp, mountPath: /tmp }]
      volumes: [{ name: tmp, emptyDir: { sizeLimit: 64Mi } }]
```

- **web (C4):** `PORT=3000`, container port 3000; `startupProbe` and `livenessProbe` on `/api/livez`, `readinessProbe` on `/api/readyz`; an extra `emptyDir` at `/app/apps/web/.next/cache`; resources `250m`/`256Mi` requests, `512Mi` limit. Its ConfigMap has `REDIS_HOST`, `REDIS_PORT`, `REDIS_USERNAME=web` and its Secret `REDIS_PASSWORD` (C12; no `REDIS_URL`), and `OTEL_EXPORTER_OTLP_ENDPOINT` is `:4318`.
- **payout-worker:** no Service; probes on its health port 8000; `terminationGracePeriodSeconds: 75`; ConfigMap `PAYOUT_CONCURRENCY=16`, `PAYOUT_CONSUMER_GC_IDLE_S=3600`, `PAYOUT_PAYROLL_BASE_URL=http://payroll:8000`, `PAYOUT_PAYROLL_CONNECT_TIMEOUT_S=1`, `PAYOUT_PAYROLL_READ_TIMEOUT_S=2`, `PAYOUT_PAYROLL_MAX_RETRIES=3` (C46), `BANK_BASE_URL`, `BANK_KEY_SAFE_WINDOW_S`; no `PAYOUT_MAX_ATTEMPTS` (C46).
- **Service:** `ClusterIP`, port 8000 (web 3000), selector by `app.kubernetes.io/name`.
- **ServiceAccount:** one per workload, `automountServiceAccountToken: false`.
- **ConfigMap `<svc>-config`:** the non-secret keys of §4 and §7.3 under their C1 names (`DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_SCHEMA`, `REDIS_HOST`, `REDIS_PORT`, `REDIS_USERNAME`, `COMPANY_ID` for every Python service including bank-stub (C47), `LOG_LEVEL: INFO`, …), including `INTERNAL_JWT_PUBLIC_KEYS` for identity (web keys only) and payroll (web and worker keys), as plan §8 requires ("ConfigMap per service (public keys)"). No `DATABASE_URL` or `REDIS_URL` (C1). Base values are the dev ones; overlays patch them.
- **Secret `<svc>-secrets`:** the secret keys under their process names (`DB_PASSWORD`, `REDIS_PASSWORD`, …); in base, placeholders (`stringData` values `REPLACE_ME`). The startup checks refuse `REPLACE_ME` in uat and prod, so a forgotten secret fails loudly.
- **PDB:** `maxUnavailable: 1` (works with 1 replica in dev and 2 in uat/prod).
- `topologySpreadConstraints` (zone, `maxSkew: 1`, `whenUnsatisfiable: ScheduleAnyway`) are added in the prod overlay.

### 9.3 Jobs

| Job | Image | Runs | Credentials | Notes |
|---|---|---|---|---|
| `db-bootstrap` | `postgres:16-alpine` (pinned) | `sh /init/lib/run-init.sh` → `for f in $INIT_FILES; do psql -v ON_ERROR_STOP=1 -f /init/$f; done` | `PGHOST`, `PGUSER`, `PGPASSWORD` (the admin) and every role password, from the `db-bootstrap` Secret | Same SQL as Compose (PAY-606), mounted from the `db-init` ConfigMap generated from the synced copy `deploy/k8s/base/bootstrap/init/` (D8, R32, C36), so `10-identity.sql`'s audit-log revoke (C50) runs on every rotation too. `INIT_FILES`: all six files in dev/uat; prod omits `40-bank-stub.sql`. Run once per environment and again to rotate passwords; `backoffLimit: 2`, `ttlSecondsAfterFinished: 86400` |
| `migrate-identity-<version>`, `migrate-payroll-<version>`, `migrate-payouts-<version>` (+ `migrate-bank-stub-<version>` in the bank-stub component) | the service image | `alembic -c /app/alembic.ini upgrade head` | the service's ConfigMap (C1 keys, `COMPANY_ID`) with `DB_USER=<schema>_migrator`, and the `<svc>-migrator` Secret (`DB_PASSWORD`) | Name per version (`nameSuffix` in `base/migrations/kustomization.yaml`, e.g. `-0-1-0`), because a Job's pod template is immutable; `backoffLimit: 1`, `activeDeadlineSeconds: 600`, `ttlSecondsAfterFinished: 604800`; same pod security context as the Deployments |
| `bootstrap-owner` | identity | `python -m identity.bootstrap_owner --if-empty --email $(OWNER_EMAIL) --company-id $(COMPANY_ID)` | identity ConfigMap with `DB_USER=identity_migrator`, `identity-migrator` Secret | PAY-100, R16. `OWNER_EMAIL` from the overlay's ConfigMap. `--if-empty` exits 0 when users exist, so the Job can be re-applied; no `podFailurePolicy`; `backoffLimit: 2`. uat and prod create a **pending** owner and email the invite; the dev overlay adds `--password-env SEED_OWNER_PASSWORD` (allowed only with `ENV=dev`) and the secret, like Compose |

**Deploy order** (RUNBOOK): `db-bootstrap` (first time or rotation) → `migrate-*-<version>` (wait for complete) → services → web → `bootstrap-owner` (first time only). Rollback: previous image tags; migrations are expand-only, so the old version keeps working.

### 9.4 NetworkPolicies

All policies select pods by `app.kubernetes.io/name`. Kubelet probes are not subject to NetworkPolicy on the common CNIs, so no probe allows are needed.

| Policy | Selects | Rule |
|---|---|---|
| `default-deny` | all pods | `policyTypes: [Ingress, Egress]`, no rules |
| `allow-dns` | all pods | egress to `kube-system` pods `k8s-app: kube-dns`, UDP and TCP 53 |
| `web` | web | ingress from the ingress-controller namespace (label set per overlay) on 3000; egress to identity:8000, payroll:8000, otel-collector:4318 |
| `identity` | identity | ingress from web on 8000; egress to otel-collector:4317 |
| `payroll` | payroll | ingress from web and payout-worker on 8000; egress to otel-collector:4317 |
| `payout-worker` | payout-worker | no ingress; egress to payroll:8000, otel-collector:4317 |
| `otel-collector` | otel-collector | ingress from pods with `app.kubernetes.io/part-of: payrun` on 4317/4318; egress per overlay (backend) |
| `migrations`, `bootstrap` | the Jobs (`app.kubernetes.io/component: migration` / `bootstrap`) | no ingress; egress only to Postgres |
| bank-stub component: `bank-stub` | bank-stub | ingress **only** from payout-worker on 8000; egress to otel-collector |
| bank-stub component: `payout-worker-to-bank-stub` | payout-worker | egress to bank-stub:8000 (dev, uat; ADR 0011 option 1) |
| in-cluster-data (dev) | postgres, redis | postgres ingress on 5432 from identity, payroll, payout-worker, bank-stub, the Jobs and postgres-exporter; redis ingress on 6379 from web, identity, payroll, payout-worker, redis-exporter; plus the matching egress allows on the clients |
| mailpit (dev, uat) | identity | egress to mailpit:1025; mailpit ingress from identity |
| managed-data-egress (uat, prod) | identity, payroll, payout-worker, bank-stub (uat), the Jobs, web (Redis only) | egress `ipBlock: {cidr: <managed Postgres CIDR>}` port 5432 and `ipBlock: {cidr: <managed Redis CIDR>}` port 6379; the CIDRs are placeholders (`# REPLACE: provider subnet`) recorded in the overlay, as ADR 0011 requires |
| bank-egress-ipblock (prod) | payout-worker only | egress `ipBlock: {cidr: 0.0.0.0/0, except: [10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16, 100.64.0.0/10]}` on TCP 443 (cluster, private, link-local and metadata ranges excluded) |
| smtp-egress (prod) | identity | egress `ipBlock` (provider CIDR placeholder) on TCP 587 |
| collector egress (uat, prod) | otel-collector | `ipBlock` to the telemetry backend's CIDR on 443 |

The External Secrets Operator's own egress to the cloud secrets API (443) lives in the operator's
namespace, which this repo doesn't manage; the RUNBOOK records it (ADR 0011). No pod in the
payrun namespaces has general internet egress.

### 9.5 Overlays

| Setting | dev | uat | prod |
|---|---|---|---|
| Namespace | `payrun-dev` | `payrun-uat` | `payrun-prod` |
| Components | bank-stub, in-cluster-data, mailpit | bank-stub, mailpit, managed-data-egress, external-secrets, ingress | managed-data-egress, bank-egress-ipblock, smtp-egress, external-secrets, ingress |
| Replicas | 1 each | 2 each (payout-worker 2) | 2 each, topology spread; **payout-worker 0** until a bank-provider ADR exists (R14, C32; asserted by `test_k8s_policy.py`) |
| Images | `payrun/<svc>:dev` (kind-loaded) | registry + `0.1.0` | registry + `0.1.0@sha256:…` (digest) |
| `ENV` | dev | uat | prod |
| `OTEL_TRACES_SAMPLER` | `parentbased_always_on` | `parentbased_always_on` | `parentbased_always_on`; the Collector's `tail_sampling` (`collector.prod.yaml`) keeps errors, > 300 ms and 10% (R27, C23) |
| `TRUSTED_PROXY_HOPS` (the web refuses < 1 in uat and prod: `config.invalid`, exit 2; C62) | 0 | 1 | 1 |
| `BANK_BASE_URL` | `http://bank-stub:8000` | `http://bank-stub:8000` | provider placeholder `https://bank-provider.invalid`; the prod startup check refuses it until a real adapter exists (plan §13), and with 0 replicas the worker never starts, so it can't crash-loop (R14) |
| `BANK_KEY_SAFE_WINDOW_S` | 86400 | 86400 | provider's key retention (placeholder) |
| SMTP | in-cluster Mailpit | in-cluster Mailpit | provider, via ExternalSecret |
| Postgres / Redis | in-cluster StatefulSets | managed (ADR 0007) | managed |
| Secrets | `secretGenerator` `payrun-dev-secrets` from `overlays/dev/secrets.env` (git-ignored, written by `dev-env.sh --k8s-dev` with the `.env` names; `manifests.sh` writes dummy values when absent). Per-service patches map the process names with `secretKeyRef`, e.g. identity `DB_PASSWORD` ← `IDENTITY_DB_PASSWORD`, `REDIS_PASSWORD` ← `REDIS_IDENTITY_PASSWORD` (C1) | ExternalSecret | ExternalSecret |
| Ingress | none (port-forward) | TLS, host placeholder | TLS, host placeholder, HSTS at the BFF |
| `LOG_LEVEL` | INFO | INFO | INFO |
| Session cookie | `sid` | `__Host-sid` | `__Host-sid` |

### 9.6 ExternalSecrets (uat, prod) and kubeconform

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata: { name: payroll-secrets }
spec:
  refreshInterval: 1h
  secretStoreRef: { kind: ClusterSecretStore, name: payrun-secrets }   # created by the user (ADR 0008)
  target: { name: payroll-secrets, creationPolicy: Owner }
  data:
    - { secretKey: DB_PASSWORD,              remoteRef: { key: payrun/<env>/PAYROLL_DB_PASSWORD } }
    - { secretKey: REDIS_PASSWORD,           remoteRef: { key: payrun/<env>/REDIS_PAYROLL_PASSWORD } }
    - { secretKey: PAYROLL_FIELD_KEYS,       remoteRef: { key: payrun/<env>/PAYROLL_FIELD_KEYS } }
    - { secretKey: PAYROLL_FIELD_ACTIVE_KID, remoteRef: { key: payrun/<env>/PAYROLL_FIELD_ACTIVE_KID } }
```

| ExternalSecret | Keys (`target key ← remote name` where they differ; remote names from plan §8; target keys are the C1 process names) |
|---|---|
| `web-secrets` | `INTERNAL_JWT_PRIVATE_KEY`, `INTERNAL_JWT_KID`, `REDIS_PASSWORD` ← `REDIS_WEB_PASSWORD`, `SESSION_SECRET` |
| `identity-secrets` | `DB_PASSWORD` ← `IDENTITY_DB_PASSWORD`, `REDIS_PASSWORD` ← `REDIS_IDENTITY_PASSWORD`, `AUDIT_IP_HASH_KEY`, `SMTP_HOST`, `SMTP_USERNAME`, `SMTP_PASSWORD` (prod; C49) |
| `payroll-secrets` | as above |
| `payout-worker-secrets` | `DB_PASSWORD` ← `PAYOUTS_DB_PASSWORD`, `REDIS_PASSWORD` ← `REDIS_PAYOUT_WORKER_PASSWORD`, `PAYOUT_WORKER_JWT_PRIVATE_KEY`, `PAYOUT_WORKER_JWT_KID`, `BANK_STUB_API_KEY` (uat; the provider's key in prod) |
| `bank-stub-secrets` (uat) | `DB_PASSWORD` ← `BANK_STUB_DB_PASSWORD`, `BANK_STUB_API_KEY` |
| `<svc>-migrator` | `DB_PASSWORD` ← `<SVC>_DB_MIGRATOR_PASSWORD` (`BANK_STUB_DB_MIGRATOR_PASSWORD` in uat only) |
| `db-bootstrap` | `POSTGRES_PASSWORD` (managed admin), every role password above, `POSTGRES_EXPORTER_PASSWORD` |
| `observability` | `ALERT_PAGE_WEBHOOK_URL`, `ALERT_TICKET_WEBHOOK_URL`, `GRAFANA_ADMIN_PASSWORD`, `REDIS_EXPORTER_PASSWORD` (consumed by the Helm-installed or hosted stack) |

`PAYROLL_FIELD_KEYS` kids and JWT kids starting `dev-`/`test-` are refused by the startup checks (ADR 0004, 0008).

**kubeconform** (`manifests.sh`, for `dev`, `uat`, `prod`):

```sh
# no load-restrictor flag (C36): the shared assets are synced copies (§3.15)
kustomize build "deploy/k8s/overlays/$env" \
  | kubeconform -strict -summary -kubernetes-version "$K8S_VERSION" \
      -schema-location default \
      -schema-location 'deploy/k8s/schemas/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
```

`deploy/k8s/schemas/external-secrets.io/` holds the JSON schemas for `ExternalSecret_v1` and
`ClusterSecretStore_v1`, converted from the pinned External Secrets Operator release's CRDs
(`openapi2jsonschema`), so CI is hermetic. `K8S_VERSION`, kustomize and kubeconform versions are
pinned in `scripts/ci/lib.sh`. Then `tests/platform/test_k8s_policy.py` checks the rendered output (§6.2).

## 10. Images

### 10.1 Python services: one shared template (`deploy/docker/python-service.Dockerfile`)

Built from the repo root with `--build-arg PACKAGE=<pkg> --build-arg SERVICE_DIR=<dir>` (`identity`,
`payroll`, `payout_worker`, `bank_stub`). `deploy/docker/bake.hcl` defines the four targets plus `web`.

```dockerfile
# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.14-slim-<debian>@sha256:<digest>    # pinned at scaffold (§10.3)
ARG UV_IMAGE=ghcr.io/astral-sh/uv:<version>@sha256:<digest>

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
ARG PACKAGE
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY libs ./libs
COPY services ./services
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package ${PACKAGE} --no-editable

FROM ${PYTHON_IMAGE}
ARG PACKAGE
ARG SERVICE_DIR
ARG VERSION=dev
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="payrun-${PACKAGE}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.source="https://github.com/Positron00/FinApp"
RUN useradd --system --uid 10001 --gid 0 --no-create-home app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY services/${SERVICE_DIR}/alembic.ini /app/alembic.ini
COPY services/${SERVICE_DIR}/migrations /app/migrations
COPY contracts /app/contracts
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    OTEL_SEMCONV_STABILITY_OPT_IN=http \
    PYCOMMON_APP=${PACKAGE}.main:app PORT=8000 \
    APP_VERSION=${VERSION}
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/livez', timeout=2).status == 200 else 1)"]
CMD ["python", "-m", "pycommon.serve"]
```

- Build and final stages both use `/app`, so the venv's console scripts (`alembic`) keep working.
- `alembic.ini` uses `script_location = %(here)s/migrations`, which resolves both in the repo (`services/<dir>/`) and in the image (`/app/`), so one file serves both.
- The worker image is the same template; `PYCOMMON_APP=payout_worker.main:app` serves its health routes and runs the consumer as a background task (D4).
- `.dockerignore` (§1.2) keeps tests, caches, `.env` and keys out of the context.

### 10.2 Web (`apps/web/Dockerfile`)

```dockerfile
# syntax=docker/dockerfile:1
ARG NODE_IMAGE=node:24-slim@sha256:<digest>

FROM ${NODE_IMAGE} AS deps
ENV PNPM_HOME=/pnpm COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN corepack enable
WORKDIR /repo
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/
COPY packages/api-client/package.json packages/api-client/
RUN --mount=type=cache,target=/pnpm/store pnpm install --frozen-lockfile --filter web...

FROM deps AS build
COPY apps/web apps/web
COPY packages packages
ENV NEXT_TELEMETRY_DISABLED=1
RUN pnpm --filter web build             # next.config: output "standalone", outputFileTracingRoot = repo root

FROM ${NODE_IMAGE}
ARG VERSION=dev
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="payrun-web" org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}"
RUN useradd --system --uid 10001 --gid 0 --no-create-home app
WORKDIR /app
COPY --from=build --chown=10001:0 /repo/apps/web/.next/standalone ./
COPY --from=build --chown=10001:0 /repo/apps/web/.next/static ./apps/web/.next/static
COPY --from=build --chown=10001:0 /repo/apps/web/public ./apps/web/public
ENV NODE_ENV=production HOSTNAME=0.0.0.0 PORT=3000 NEXT_TELEMETRY_DISABLED=1 APP_VERSION=${VERSION}
USER 10001
EXPOSE 3000
CMD ["node", "apps/web/server.js"]
```

No secrets are needed at build time (the JWT key is loaded lazily, `auth.md`); runtime config is env only.

### 10.3 Base images and pins

**Policy:** every image is the **current stable release that is at least 2 weeks old** on the
scaffold date, pinned by version tag **and** digest (`name:tag@sha256:…`), and verified at scaffold
time by pulling it for both `linux/arm64` (local Apple silicon) and `linux/amd64` (CI) and recording
the digest. Major lines fixed by an ADR are kept (Postgres 16, Redis 7). Dependabot (`docker`
ecosystem in `.github/dependabot.yml`) proposes updates weekly; an update is merged only once the
new version is 2 weeks old and CI (including Trivy) is green.

| Image | Line | Used by | Why this line |
|---|---|---|---|
| `python` | `3.14-slim` (Debian stable) | 4 services, alert-sink | Newest minor with a full year of wheels for every dependency (asyncpg, argon2-cffi, cryptography, pydantic-core); the scaffold check is `uv sync --locked` on both architectures with no source builds. Gives stdlib `uuid.uuid7` |
| `ghcr.io/astral-sh/uv` | current release | build stage | — |
| `node` | `24-slim` (Active LTS) | web | Current LTS |
| `postgres` | `16-alpine` | Compose, Testcontainers, db-bootstrap Job | ADR 0007 (psql 16 gives `\getenv`) |
| `redis` | `7-alpine` (7.4.x) | Compose, Testcontainers, dev K8s | ADR 0007 (ACL selectors need ≥ 7.0) |
| `otel/opentelemetry-collector-contrib` | current release | obs | contrib has the prometheus exporter, transform and tail_sampling |
| `prom/prometheus`, `prom/alertmanager` | current 3.x / 0.x | obs, `promtool`/`amtool` in CI | same image in CI and Compose |
| `grafana/grafana`, `grafana/tempo`, `grafana/loki` (3.x) | current releases | obs | Loki 3 for native OTLP |
| `quay.io/prometheuscommunity/postgres-exporter`, `oliver006/redis_exporter` | current releases | obs | redis_exporter for `--check-single-streams` |
| `axllent/mailpit` | current release | core | — |
| `ghcr.io/shopify/toxiproxy` | current 2.x | test | — |
| `ghcr.io/yannh/kubeconform`, `zricethezav/gitleaks`, `aquasec/trivy` | current releases | CI tools | run as containers so versions are pinned like images |

## 11. CI (`.github/workflows/`; PAY-NFR-12)

### 11.1 `ci.yml`

```yaml
name: ci
on:
  pull_request:
  push: { branches: [main] }
permissions: { contents: read }
concurrency:
  group: ci-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
env:
  UV_FROZEN: "1"
  FORCE_COLOR: "1"
jobs:
  python:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    strategy:
      fail-fast: false
      matrix: { member: [pycommon, identity, payroll, payout_worker, bank_stub, platform] }
    steps:
      - uses: actions/checkout@<sha>        # every action pinned to a full commit SHA, with a version comment
      - uses: astral-sh/setup-uv@<sha>
        with: { version: "<pinned uv>", enable-cache: true, cache-dependency-glob: uv.lock }
      - run: uv python install              # reads .python-version
      - run: scripts/ci/python.sh ${{ matrix.member }}
      - uses: actions/upload-artifact@<sha>
        if: always()
        with: { name: coverage-${{ matrix.member }}, path: coverage.xml, if-no-files-found: ignore }
  # web, contract, e2e, images, manifests, security: see the table
```

| Job | Runs on / timeout | Steps | Script | Caches |
|---|---|---|---|---|
| `python` (matrix: `pycommon`, `identity`, `payroll`, `payout_worker`, `bank_stub`, `platform`) | ubuntu-latest (Docker for Testcontainers) / 20 min | checkout, setup-uv, `uv python install`, script, upload coverage | `python.sh <member>` | uv cache keyed on `uv.lock` |
| `web` | ubuntu-latest / 15 min | checkout, setup-node (`.nvmrc`), corepack, script, upload coverage | `web.sh` | pnpm store keyed on `pnpm-lock.yaml`; `apps/web/.next/cache` |
| `contract` | ubuntu-latest / 15 min | checkout with `fetch-depth: 0` (needs `origin/main`), setup-uv, setup-node, script | `contract.sh` | uv, pnpm |
| `e2e` (env `E2E_SUITE: skeleton` until F3, then `full`; C51) | ubuntu-latest / 40 min | checkout, setup-uv, setup-node, `docker/setup-buildx-action`, install k6 (pinned) and Playwright browsers (`pnpm exec playwright install chromium`; no `--with-deps`, C77), script; `if: failure()` upload `playwright-report/`, `ci-logs/`; `if: always()` `stack.sh down -v` | `e2e.sh` | uv, pnpm, Playwright browsers keyed on the Playwright version, buildx `type=gha` per image |
| `images` | ubuntu-latest / 30 min | checkout, setup-buildx, script (`.trivyignore` policy check, build `linux/amd64`, Trivy) | `images.sh` | buildx `type=gha`; Trivy DB cache |
| `manifests` | ubuntu-latest / 10 min | checkout, setup-uv, script (k8s asset sync diff, kustomize, kubeconform, k8s policy test, promtool, amtool, collector validate, compose config) | `manifests.sh` | uv |
| `security` | ubuntu-latest / 10 min | checkout with `fetch-depth: 0`, setup-uv, setup-node, script (pip-audit, pnpm audit, gitleaks over `base..head`) | `security.sh` | uv, pnpm |

- **Permissions:** `contents: read` everywhere; no job needs more. No repository secrets: jobs that run the stack call `scripts/dev-env.sh` first (ADR 0008).
- **Required checks** (branch protection is the user's to set): all seven jobs.
- `e2e` builds the images itself with the same bake file and cache scopes as `images`, so the second build is mostly cache hits.
- Images are built for `linux/amd64` in CI (PAY-NFR-13).

### 11.2 `nightly.yml`

```yaml
name: nightly
on:
  schedule: [{ cron: "17 3 * * *" }]     # 03:17 UTC daily
  workflow_dispatch:
permissions: { contents: read }
concurrency: { group: nightly, cancel-in-progress: false }
```

| Job | Timeout | Script | Proves |
|---|---|---|---|
| `load` | 100 min (nominal ~55; measured 46 min in run 43040233170) | `nightly-load.sh` | full 30 req/s × 10 min, soak 5 req/s × 30 min with no latency drift, no memory growth and no consumer-lag growth (`soak_resources.py`), settlement p95 ≤ 60 s for 500 employees (PAY-NFR-05, 06) |
| `resilience` | 60 min (measured 33 min in run 43040233170) | `nightly-resilience.sh` | every fault in plan §12.1, each with its HLD §4 log event and metric (PAY-NFR-02, 03) |
| `e2e-repeat` | 120 min (3 × the PR `e2e` job's 40 min; measured 28 min in run 43074520518) | `nightly-e2e.sh` | the full e2e three times in a row (flakiness) |
| `schemathesis-deep` | 45 min (measured 8.5 min in run 43040233170) | `nightly-schemathesis.sh` | 500 examples per operation |
| `audit` | 20 min (measured 2.5 min in run 43040233170) | `nightly-audit.sh` | pip-audit, pnpm audit and Trivy against fresh advisories |

The measured durations are from the nightly runs on `release/0.1.0` (43040233170; `e2e-repeat` from
43074520518, the first run where it could start, C77). The first full pass of all five jobs is
43074520518 (2026-11-11: load 47 min, resilience 35 min, e2e-repeat 28 min). Every timeout keeps
room for a slower runner, so none changed.

**Failure issues:** each job tees its script output to `ci-logs/<job>.log` and uploads it as the
artifact `log-<job>` (`if: always()`); only these jobs hold `contents: read`. A separate job,
`report`, runs after all five (`needs: [load, resilience, e2e-repeat, schemathesis-deep, audit]`,
`if: !cancelled() && contains(needs.*.result, 'failure')`). It is the only job with
`permissions: {contents: read, issues: write}`. It downloads the `log-*` artifacts into
`nightly-logs/` and, for each job whose result is `failure`, finds that job's log with
`find nightly-logs/log-<job> -name <job>.log -print -quit` (an artifact is rooted at the common
ancestor of its upload paths, so `load`, which also uploads `load/results/`, has its log at
`log-load/ci-logs/load.log`), falling back to `nightly-logs/log-<job>/<job>.log`, and runs
`scripts/ci/report_failure.sh <job> <log>` with `GH_TOKEN: ${{ github.token }}`. The script:
1. looks for an open issue labelled `nightly-failure` titled `Nightly failure: <job>` (`gh issue list --label nightly-failure --state open --search "in:title \"Nightly failure: <job>\"" --json number`);
2. builds a body with the run URL, the commit SHA, and the last 150 lines of the log, passed through `scripts/ci/lib.sh`'s `scrub` (masks anything matching the redactor's JWT/PEM/Bearer patterns, though CI uses throwaway keys);
3. comments on the issue if it exists, otherwise creates it (`gh issue create --label nightly-failure`), creating the label on first use.
A green nightly doesn't close the issue; a person closes it once the cause is fixed (testing.md: a flake is a bug).

## 12. Gates (PAY-NFR-12)

All gates are at their final thresholds from the walking skeleton on, and run in CI and locally
through the same scripts.

| Gate | Command | Threshold | CI job |
|---|---|---|---|
| Python lint | `ruff check <paths>` | 0 errors | python |
| Python format | `ruff format --check <paths>` | no diff | python |
| Python types | `mypy --strict <src> <tests>` | 0 errors | python |
| Service coverage | `pytest … --cov=<pkg> --cov-branch --cov-append --cov-fail-under=85` (unit + integration); the same threshold is `fail_under = 85` in each service's `pyproject.toml`, so a local run from the member directory enforces it too (C63) | ≥ 85% lines, per service | python |
| pycommon coverage | same, `--cov=pycommon --cov-fail-under=90`; `fail_under = 90` in `libs/pycommon/pyproject.toml` (C63) | ≥ 90% lines | python |
| Web lint / format / types | `pnpm --filter web lint`, `prettier --check .`, `pnpm --filter web typecheck` and `pnpm --filter @payrun/api-client typecheck` (`tsc --noEmit`) | 0 errors | web |
| Web coverage | Vitest `coverage.thresholds.lines = 80` | ≥ 80% lines | web |
| Web integration (real Redis) | `pnpm --filter web int` (`vitest.int.config.ts`, Testcontainers; Docker needed; web LLD §9.1) | all pass | web |
| Web build | `pnpm --filter web build` | succeeds | web |
| Contract drift | `export_openapi.py --all` + `pnpm --filter @payrun/api-client generate` + `git diff --exit-code` | no diff | contract |
| Breaking changes | `oasdiff breaking <main spec> <PR spec> --fail-on ERR` (skipped when `main` has no spec) | none | contract |
| Event schemas | `check-jsonschema --check-metaschema` + examples; producer and consumer tests validate envelopes | pass | contract, python |
| API fuzz | `schemathesis run … --checks all` per service | no 5xx, no schema violations | e2e |
| End to end | Playwright: `@skeleton` while `E2E_SUITE=skeleton`, then `@journeys` + trace test + axe (C51) | all pass; no serious/critical axe violations | e2e |
| Fault smoke (C38) | `pytest tests/resilience -m smoke`: the PR Toxiproxy tests (slow DB, identity down), each also checking its HLD §4 metric in Prometheus | pass | e2e |
| Seed (PAY-609) | `pytest tests/platform/test_seed.py -m stack` + `test_mint_token.py` | pass | e2e, python (platform) |
| Start-up (PAY-NFR-15) | `stack.sh up --check-startup` | every service ready ≤ 20 s after its migrations | e2e |
| Load smoke | `k6 run load/smoke.js` (5 VUs × 30 s) | p95 < 300 ms, errors < 1% | e2e |
| Metric names | `check_metrics.sh` (26 names, §3.15) | every required series present | e2e |
| Images | `trivy image --severity HIGH,CRITICAL --exit-code 1 --ignorefile .trivyignore` | no HIGH or CRITICAL | images |
| Trivy ignore policy (C38) | `images.sh` policy check | every `.trivyignore` entry has a reason, an owner and an unexpired `exp:YYYY-MM-DD`, and is in `SECURITY_DEPENDENCIES.md` | images |
| Python deps | `pip-audit --strict` | none, except accepted in `SECURITY_DEPENDENCIES.md` | security |
| JS deps | `pnpm audit --audit-level high` | none above moderate, except accepted | security |
| Secrets | `gitleaks git --log-opts="<base>..HEAD"` | nothing found | security |
| Alert rules (C38) | `promtool check rules`, `promtool test rules` (every alert has a firing and a quiet case) | pass | manifests |
| Prometheus config | `promtool check config` | pass | manifests |
| Alertmanager (C38) | `amtool check-config` | pass | manifests |
| Collector | `otelcol-contrib validate --config` (both files) | pass | manifests |
| Kubernetes | `kustomize build … \| kubeconform -strict -summary` for dev, uat, prod (with CRD schemas; no load-restrictor flag) + `test_k8s_policy.py` | pass | manifests |
| K8s asset copies (R32, C36) | `sync-k8s-assets.sh` + `git diff --exit-code` on the copies | no diff | manifests |
| Compose | `docker compose config -q` (base and test override) + `test_compose.py` | pass | manifests, python (platform) |
| Nightly load | `load/full.js`, `load/soak.js`, `load/settlement.js` | p95 < 300 ms (heavy < 1.5 s), errors < 1%, no memory or lag growth in soak (C38): RSS slope < 5% per Python service and no `queue_consumer_lag` both > 0 and rising, checked in Prometheus by `soak_resources.py`; settlement p95 ≤ 60 s | nightly |
| Nightly failure issue (C38) | `report_failure.sh <job>` (§11.2) | a failed nightly job opens or updates its `nightly-failure` issue with the log tail | nightly |

`docs/TESTING.md` records the table and the latest measured values (PAY-NFR-12 check).

## 13. Traceability

| Requirement | Platform modules / assets | Tests |
|---|---|---|
| PAY-100 (bootstrap owner runs as migrator) | Compose `bootstrap-owner` and K8s `bootstrap-owner` Job with `--if-empty` (R16), identity migrator role | `test_compose.py`; identity `test_bootstrap_owner.py` |
| PAY-110 (email via resilient client) | `http.Guard` (`provider="smtp"`) | `test_http.py`, `test_breaker.py` |
| PAY-402 (outbox and stream) | `streams.Envelope`, `StreamProducer`, `RelayLoop` over payroll's `OutboxRepository` (payroll owns the table; C22); Redis ACL for payroll | `test_relay_loop.py`, `test_streams.py`, `test_streams_unit.py`, `test_redis_acl.py` |
| PAY-403, 408 (consumer mechanics) | The worker's own consumer (C21); pycommon gives `create_redis` for the blocking client and the traceparent helpers; worker ACL with `XGROUP DELCONSUMER` (C27) | `test_streams.py`, `test_redis_acl.py`; worker suite |
| PAY-404 (bank classification input) | `http.ResilientClient.request_raw` | `test_http.py` |
| PAY-407 (re-publish runner) | `maintenance.MaintenanceRunner`, `startup_checks.check_republish_after` | `test_maintenance.py`, `test_startup_checks.py` |
| PAY-409 (poison → DLQ) | DLQ schema `contracts/events/payout.requested.dlq.v1.json` (C21); worker ACL `XADD` on the DLQ | `contract.sh`, `test_redis_acl.py`; worker suite |
| PAY-412 (bank-stub ledger) | `bank_stub` schema and roles, Toxiproxy `bank-stub` proxy | `test_bootstrap_sql.py`; bank-stub suite; resilience |
| PAY-600 Internal JWT | `auth.Keyset`, `TokenVerifier`, `TokenSigner`; `dev-env.sh` keysets; per-service ConfigMaps | `test_jwt.py`, `test_dev_env.py` |
| PAY-601 Authorization | `auth.require` (the C30 401/403 rule), `create_app` router wiring | `test_auth_deps.py`, `test_app.py` |
| PAY-602 Error format | `errors.*`, `install_handlers` | `test_errors.py` |
| PAY-603 Rate limits | `ratelimit.RateLimiter`, `caller_rate_limit(exempt_subs=("service:web",))` (C17), `compose.test.yaml` raised limits | `test_ratelimit.py`, `test_ratelimit_unit.py` |
| PAY-604 Body limits, request id headers | `app.py` middleware (`MAX_BODY_BYTES`, `x-request-id`, `x-trace-id`) | `test_app.py` |
| PAY-605 Health and startup | `health.py`, `startup_checks.py` (exit 2, `startup.check_failed`; C29), `db.py` timeouts; web `/api/livez`, `/api/readyz` in Compose and K8s (C4) | `test_health.py`, `test_startup_checks.py`, `test_db_pg.py`, `test_compose.py`, `test_k8s_policy.py` |
| PAY-606 Data ownership | `deploy/compose/init/*` (with the C50 audit-log revoke), `pycommon.testing.pg_bootstrapped`, K8s `db-bootstrap` Job from the synced copy (R32), `run_alembic_env` | `test_bootstrap_sql.py`, `test_sync_k8s_assets.py`; each service's `test_grants.py` |
| PAY-607 Redis ACLs | `deploy/compose/redis/*`, `redis_client.create_redis` | `test_redis_acl.py`; per-service ACL tests |
| PAY-608 Maintenance and retention | `maintenance.MaintenanceRunner` (services supply the purge tasks) | `test_maintenance.py`, `test_maintenance_unit.py`; each service's `test_maintenance.py` |
| PAY-609 Seed data | `scripts/seed.py` (`--skeleton`, JSON summary; C33), `scripts/mint_token.py`, `scripts/ci/schemathesis_auth.py` (C35), `bootstrap-owner --if-empty --password-env`, `dev-env.sh` seed passwords | `test_seed.py`, `test_mint_token.py`; the e2e global setup |
| PAY-NFR-02, 03 (no double payment, liveness) | Toxiproxy proxies, `compose.test.yaml`, relay mechanics | `test_relay_loop.py`; `tests/resilience` (smoke in PR, all nightly) |
| PAY-NFR-04 Availability SLO | `recording.yml` `sli:bff_errors:*`, burn-rate alerts | `slo_test.yml`, `alerts_test.yml` |
| PAY-NFR-05 Latency SLO | `sli:bff_latency_*`, k6 smoke in `e2e.sh`, nightly full and soak | k6 thresholds |
| PAY-NFR-06 Settlement SLO | `SETTLEMENT_BUCKETS`, `sli:payout_settlement_p95:1h`, nightly `settlement.js` | `slo_test.yml`; nightly load |
| PAY-NFR-07 No secrets in telemetry | `logging.redact`, `RedactingFilter`, OTel hooks, Collector `transform/redact` | `test_logging.py`, `test_otel.py`, `test_secret_not_logged.py`, `test_jwt.py`; e2e Loki/Tempo token search |
| PAY-NFR-08 Privacy | hashed subjects in limiter keys, no PII metric labels | `test_ratelimit_unit.py`, `test_dashboards.py` |
| PAY-NFR-09 Tracing | `parentbased_always_on` everywhere (C23), `otel.inject_traceparent` / `new_traceparent` / `context_from_traceparent`, required `Envelope.traceparent` (C22), Collector (prod `tail_sampling`), Tempo, Loki | `test_streams.py` (same trace id), `test_otel.py`; e2e `trace.spec.ts` |
| PAY-NFR-10 Observability assets | `otel.py` naming rule, Collector, Prometheus, rules, Alertmanager, alert-sink, Grafana, five dashboards, 18 runbooks | `test_rules_meta.py`, `test_dashboards.py`, `test_alert_sink.py`, `check_metrics.sh`, promtool, amtool; Step 6 drills |
| PAY-NFR-11 Paging alerts | `PayoutDLQ`, `PayoutsStuck`, `PayoutNeedsOperator`, `PayoutSettlementSlow` rules; DLQ counter pre-initialisation | `alerts_test.yml` fire and quiet cases |
| PAY-NFR-12 Quality gates | `ci.yml`, `scripts/ci/*.sh` (promtool/amtool, fault smoke, Trivy ignore policy, nightly failure issue; C38), root `pyproject.toml` (ruff, mypy, coverage) | §12 |
| PAY-NFR-13 Deployability | Compose profiles and override, Dockerfiles, Kustomize base, components, overlays (prod worker replicas 0; C32), Jobs, NetworkPolicies, `sync-k8s-assets.sh` | `test_compose.py`, `test_k8s_policy.py`, kubeconform, `stack.sh up --wait` in e2e |
| PAY-NFR-15 Recovery and startup | Redis AOF (`redis.conf`), `stack.sh up --check-startup`, `start_period: 20s` health checks | `ci-logs/startup.json` in e2e; resilience `test_redis_restart.py` |
| PAY-NFR-14 Accessibility | e2e job wiring (axe runs in Playwright) | web e2e |

## Change log

- 2026-11-10: incorporated README R-table and C-table decisions (R1, R2, R12, R14, R16, R22, R25, R26, R27, R28, R29, R30, R31, R32; C1, C2, C3, C4, C10, C12, C17, C18, C19, C20, C21, C22, C23, C24, C27, C28, C29, C30, C32, C33, C35, C36, C38, C44, and the later lead decisions C46, C47, C49, C50, C51 and the `not_deployable` startup-check reason).
- 2026-11-10 (Step 5): folded in C53–C63. C53 settles the worker timing in the test stack: `compose.test.yaml` keeps the shared `x-payout-timing` values, and only in-process integration fixtures lower them. C58 (§1.2 tool settings, pytest-socket), C59 (`check_secrets`, `export_openapi.py`), C60 (`55P03`), C61 (SQLAlchemy `skip_dep_check`, UUIDv7 `jti`, OTel test isolation) and C63 (gauge units, DB-only error mapping and 22021, `fail_under` per member, `alembic_version` re-revoke, `-scan -randomkey`, fatal JWT keyset check, runtime test secrets, `outbox.relay.published`, `RateLimiter.record_denied` / `record_error`, `--k8s-dev-file`). §1.2 now matches the committed workspace files.
- 2026-11-11 (release review fixes): §3.15 `e2e.sh` row matches the script and C70 (`full` in ci.yml and nightly.yml since F6; seed tests and k6 smoke **before** Schemathesis; seed tests and fault smoke must run with no skips). `nightly-load.sh` row lists `LOAD_SCENARIOS`, `FULL_HOLD`, `SOAK_DURATION`, the drift check, the new soak resource check (`soak_resources.py`: RSS slope < 5% per Python service, no consumer-lag growth; PAY-NFR-05) and the settlement Prometheus cross-check; `nightly-e2e.sh` row updated. Seed section matches `seed.py` (`random.Random(SEED*1000+i)`, the 500-mode shapes, sign-in first and list users only on a 409) and the `test_seed.py` row covers the full and 500 modes. `OTEL_EXPORTER_OTLP_TIMEOUT` (seconds, Python only, 3; C67) added to §3.3 exporters, §4 and the Compose / Kubernetes excerpts; `test_k8s_policy.py` checks it. §11.2 timeouts: `load` 100 min, `e2e-repeat` 120 min (unmeasured on runners; H2).
- 2026-11-11 (alignment review fixes): §1.1 tree lists `CLAUDE.md`, `docs/API.md`, `deploy/k8s/observability/` and `scripts/ci/{k6.sh,schemathesis.sh,schemathesis.toml,alert_drills.sh}`; `.trivyignore` holds 17 accepted base-image CVEs (C66); `.env.example` documents the keys `dev-env.sh` writes (other keys take code defaults). §3.15 `web.sh` row and §12 add `pnpm --filter @payrun/api-client typecheck` and `pnpm --filter web int` (Docker); `check_metrics.sh` row lists the script's 26 names (8 added: email failures and age, Redis memory, DLQ length, process RSS, sign-ins, external-call histogram). §8.1 `metric_expiration: 2m` (C72), prod `expected_new_traces_per_sec: 100` as in `collector.prod.yaml`, and the Collector is one replica in every overlay (uat and prod had 2; tail sampling needs whole traces), asserted by `test_k8s_policy.py`. §8.3: the C75 new-series arm is `unless max_over_time(X[1h] offset 15m)` (EmailOutboxStuck `[2h] offset 1h`), so an old series behind a data gap no longer pages; promtool gap cases for all three, a PayoutLateSettlement row in the promtool table, CollectorDropping copied from `alerts.yml` (the `(_total)?` regex) with flat-counter quiet cases; EmailOutboxStuck links to `payrun-queues`; `dashboard_url` is the dev host and other environments must template it (the "overlays rewrite the host" claim was wrong). §11.2 describes the separate `report` job and how it finds each log, and records the measured nightly durations (run 43040233170); timeouts unchanged. §6.1 adds the relay-crash integration test; §3.15 `nightly-resilience.sh` row: each fault test checks its HLD §4 log event and metric, and the payout timing comes from `docker compose config`.

Nothing is open.
