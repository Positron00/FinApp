# PayRun

PayRun is payroll for small and medium businesses. A company's staff invite each other, keep an
employee list, and run payroll each period: create a run, enter hours, calculate pay with exact
integer-cent rules, review the payslips and approve with dual control. Each employee's net pay
then goes out as a separate payout to a simulated bank, and failed payouts can be fixed and
retried. No employee may ever be paid twice, and every payout must reach a final state
([ADR 0003](docs/adr/0003-payout-flow.md)).

Version 0.1.0 is the first release. Its scope and out-of-scope list are in the
[spec README](docs/specs/payrun-0.1/README.md#scope).

## User journeys

The five journeys of 0.1.0 (plan §3, [`docs/plan/architecture-plan.md`](docs/plan/architecture-plan.md)):

| ID     | Journey                | What a user does                                                                                                                                             |
| ------ | ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **J1** | Access                 | An admin invites a colleague by email with chosen roles; the invitee sets a password from the emailed link and signs in. Users sign out and reset passwords. |
| **J2** | Company setup          | An admin sets the tax and retirement rates; a payroll admin adds, edits and deactivates salaried or hourly employees with bank details.                      |
| **J3** | Prepare a pay run      | A payroll admin creates a run for a schedule (monthly or biweekly) and period, enters hours, calculates, and reviews the summary and payslips.               |
| **J4** | Approve and pay        | An approver approves a calculated run; payouts are processed in the background until the run is `paid` or `partially_failed`.                                |
| **J5** | Recover failed payouts | The approver sees which payouts failed and why; a payroll admin fixes the bank details; the approver retries.                                                |

Roles are `admin`, `payroll_admin` and `approver`; a user may hold several
([ADR 0002](docs/adr/0002-sign-in-and-roles.md)).

## Prerequisites

| Tool          | Version                                                                           | Where it is pinned                                       |
| ------------- | --------------------------------------------------------------------------------- | -------------------------------------------------------- |
| uv            | 0.12.13                                                                           | `UV_VERSION` in `.github/workflows/ci.yml`               |
| Python        | 3.14 (uv installs 3.14.x: `uv python install`)                                    | `.python-version`, `requires-python` in `pyproject.toml` |
| Node.js       | 24 (CI); `>=22` is accepted locally                                               | `.nvmrc`, `engines` in `package.json`                    |
| pnpm          | 10.34.9, through corepack (`corepack enable`)                                     | `packageManager` in `package.json`                       |
| Docker        | Docker Engine or Desktop with Compose v2 (`up --wait`)                            | —                                                        |
| OpenSSL 3     | needed by `scripts/dev-env.sh` for Ed25519 keys (macOS: `brew install openssl@3`) | —                                                        |
| k6 (optional) | 1.8.2; `scripts/ci/k6.sh` falls back to the `grafana/k6` image                    | `.github/workflows/ci.yml`                               |

## Quick start

```sh
corepack enable
uv sync --all-packages             # Python workspace (libs/*, services/*)
pnpm install                       # web and api-client

scripts/dev-env.sh                 # writes the git-ignored .env (mode 0600) with fresh secrets
scripts/stack.sh up                # builds the images, starts core,obs,test and waits until healthy
scripts/stack.sh seed              # seeds users, settings, 10 employees and two runs (prints JSON)
```

Then open <http://127.0.0.1:4100> and sign in (see [Seeded sign-ins](#seeded-sign-ins)). Use
`127.0.0.1`, not `localhost`: the BFF checks the Origin of every mutation against
`PUBLIC_BASE_URL` (spec C12).

- `scripts/stack.sh up --profiles core` starts only the app, Postgres, Redis and Mailpit.

> **The default stack is a test stack.** `scripts/stack.sh up` turns on the `test` profile, which routes Postgres and Redis through Toxiproxy, publishes the service, Prometheus, Alertmanager and alert-sink ports, and raises every rate limit and the login lockout to 1,000,000 (`deploy/compose/compose.test.yaml`). To try the production limits (PAY-104, PAY-603, C74) by hand, use `scripts/stack.sh up --profiles core,obs`; those ports are then not published.

- `scripts/stack.sh seed --employees 500` adds monthly employees up to 500 (for k6);
  `scripts/stack.sh seed --skeleton` seeds only the walking-skeleton set.
- `scripts/stack.sh down` stops everything; `scripts/stack.sh reset` drops the volumes and starts
  again. More in [`docs/RUNBOOK.md`](docs/RUNBOOK.md).

## URLs and ports

Every port is bound to `127.0.0.1` at an offset from `APP_PORT_BASE=4100`, computed by
`scripts/dev-env.sh` and published by `deploy/compose/compose.yaml` (web, Grafana, Mailpit) and
`deploy/compose/compose.test.yaml` (the rest, with the `test` profile, which `stack.sh up` enables
by default).

| Port | Service             | URL / use                                                                           |
| ---- | ------------------- | ----------------------------------------------------------------------------------- |
| 4100 | web (pages and BFF) | <http://127.0.0.1:4100>; health `/api/livez`, `/api/readyz`                         |
| 4101 | Grafana             | <http://127.0.0.1:4101> (user `admin`, password `GRAFANA_ADMIN_PASSWORD` in `.env`) |
| 4102 | Mailpit UI          | <http://127.0.0.1:4102> (invite and reset emails)                                   |
| 4110 | identity            | `/livez`, `/readyz`, `/v1/...` (internal JWT needed)                                |
| 4111 | payroll             | `/livez`, `/readyz`, `/v1/...` (internal JWT needed)                                |
| 4112 | payout-worker       | `/livez`, `/readyz` only                                                            |
| 4113 | bank-stub           | `/livez`, `/readyz`, `/v1/transfers` (`X-Api-Key`)                                  |
| 4130 | Tempo               | traces API                                                                          |
| 4131 | Loki                | logs API                                                                            |
| 4132 | Prometheus          | <http://127.0.0.1:4132>                                                             |
| 4133 | Alertmanager        | <http://127.0.0.1:4133>                                                             |
| 4134 | Toxiproxy API       | fault injection for tests                                                           |
| 4135 | alert-sink          | `GET /page`, `GET /ticket`: alerts received                                         |

## Seeded sign-ins

`scripts/stack.sh up` creates the owner (the Compose `bootstrap-owner` one-shot), and
`scripts/stack.sh seed` creates the other two through the admin invite API and Mailpit
(`scripts/seed.py`). The passwords are generated by `scripts/dev-env.sh` and live only in your
git-ignored `.env`; look them up there.

| Email                         | Roles                                | Password (key in `.env`) |
| ----------------------------- | ------------------------------------ | ------------------------ |
| `owner@payrun.example.com`    | `admin`, `payroll_admin`, `approver` | `SEED_OWNER_PASSWORD`    |
| `approver@payrun.example.com` | `approver`                           | `SEED_APPROVER_PASSWORD` |
| `load@payrun.example.com`     | `payroll_admin` (used by k6)         | `LOAD_USER_PASSWORD`     |

The full seed turns dual approval on, so the owner prepares and calculates runs and the approver
approves them ([ADR 0005](docs/adr/0005-four-eyes-approval.md), amended by spec C73). Employee 9
has an account ending `0000`, which the bank-stub always rejects (used by J4 and J5). `seed.py`
refuses to run unless `ENV=dev`.

## Repository layout

```text
apps/web/                Next.js 16 pages and BFF route handlers (src/app/api/**/route.ts), Playwright e2e
packages/api-client/     TypeScript types generated from identity's and payroll's openapi.json
services/identity/       users, credentials, invites, resets, roles, auth audit, email relay
services/payroll/        settings, employees, runs, calculation, approval, payouts, outbox, re-publish
services/payout_worker/  consumes payout.requested, calls the bank, reports results
services/bank_stub/      simulated bank with a persistent idempotent ledger (dev and uat only)
libs/pycommon/           shared Python: settings, logging, OTel, errors, JWT, health, DB, streams, limiter
contracts/events/        payout.requested.v1 and DLQ entry JSON Schemas, with examples
deploy/compose/          compose.yaml (profiles core, obs, test), compose.test.yaml, bootstrap SQL, Redis ACLs
deploy/observability/    Collector, Prometheus rules and tests, Alertmanager, Grafana dashboards, alert-sink
deploy/k8s/              Kustomize base, components, and dev/uat/prod overlays
deploy/docker/           Python service Dockerfile and the buildx bake file
load/                    k6 scenarios: smoke, full, soak, settlement
tests/platform/          tests of the deploy assets and scripts
tests/resilience/        fault-injection tests against the running stack
scripts/                 dev-env.sh, stack.sh, seed.py, mint_token.py, export_openapi.py, sync-k8s-assets.sh, ci/*.sh
docs/                    plan, specs, ADRs, runbooks, and the guides below
```

## Security

The security design is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and the spec
(PAY-600–609, ADRs 0002, 0004, 0008, 0009, 0011). In short:

- Browsers hold only an HttpOnly session cookie. The BFF checks the Origin of every mutation, sets a
  nonce-based CSP, rate-limits by session and client IP, and calls each service with a 60 s EdDSA
  JWT for that service's audience.
- Bank account numbers are encrypted (AES-256-GCM) and only the last 4 digits are ever shown.
  Payouts follow ADR 0003, so no employee is paid twice, and approval needs two people when dual
  approval is on (ADR 0005, amended by C73).
- Each service has its own Postgres schema, roles and Redis ACL user. Audit tables are
  append-only for the services.
- Containers run as non-root on a read-only root filesystem with every capability dropped.
  Kubernetes uses default-deny NetworkPolicies. Real secrets never live in the repo: `.env` is
  generated by `scripts/dev-env.sh` and git-ignored, and uat and prod use External Secrets.

### Supply chain and accepted CVEs

CI fails on any Python advisory (pip-audit `--strict`), any JS advisory at `high` or above
(`pnpm audit`), any HIGH or CRITICAL CVE in our images (Trivy) and any leaked secret (gitleaks).
The nightly `audit` job re-runs the dependency and image scans against fresh advisory databases.

All five images use Debian 13 (trixie) bases: `python:3.14.9-slim-trixie` for the services and
`node:24.22.0-trixie-slim` for the web (C78). The Dockerfiles install Debian's security updates
and remove pip, npm and corepack from the runtime images.

**11 HIGH CVEs are accepted, and no CRITICAL ones.** All 11 are in Debian 13 packages of the base
images that Debian has not fixed yet: util-linux and mount tools, SQLite, ncurses, systemd
client libraries, libacl and perl-base. None of that code is reachable from the running services:
they never run those tools or load those libraries, and the pods lack the privileges the tools
need. Each acceptance has a reason, an owner and an expiry date (2027-01-10) in
[`SECURITY_DEPENDENCIES.md`](SECURITY_DEPENDENCIES.md) and `.trivyignore`. On expiry the Trivy gate
fails again: rebuild on a patched base image if Debian has shipped a fix, or re-review and renew the
acceptance. No Python or JS advisory is accepted.

To check locally:

```sh
scripts/ci/images.sh     # builds the images, checks the .trivyignore policy, runs Trivy
scripts/ci/security.sh   # pip-audit, pnpm audit, gitleaks (GITLEAKS_BASE=<sha> limits it to new commits)
```

## Documentation

- [`docs/WORKFLOW-PAYROLL.md`](docs/WORKFLOW-PAYROLL.md): a pay period end to end, from setup to payouts and recovery: who does each step and what the system does.
- [`docs/WORKFLOW-ENGINEERING.md`](docs/WORKFLOW-ENGINEERING.md): the engineering workflow, from setup and local gates to CI, nightly and release.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): service map, request and payout paths, data ownership.
- [`docs/API.md`](docs/API.md): every service endpoint and BFF route, error codes, event contracts.
- [`docs/TESTING.md`](docs/TESTING.md): every test layer and gate, and measured results.
- [`docs/RUNBOOK.md`](docs/RUNBOOK.md): operating the stack, alerts and runbooks, common operations.
- [`docs/plan/architecture-plan.md`](docs/plan/architecture-plan.md): the architecture plan.
- [`docs/specs/payrun-0.1/`](docs/specs/payrun-0.1/README.md): requirements, designs, tasks and the
  decision tables (R1–R37, C1–C79).
- [`docs/adr/`](docs/adr/): ADRs 0001–0013.
- [`docs/runbooks/`](docs/runbooks/): one runbook per alert.
- [`SECURITY_DEPENDENCIES.md`](SECURITY_DEPENDENCIES.md): accepted dependency and image CVEs.
- [`CHANGELOG.md`](CHANGELOG.md) and [`CLAUDE.md`](CLAUDE.md) (conventions for contributors and AI sessions).

## Licence

There is no `LICENSE` file in this repository, so no licence has been granted yet: the code is
not open source, even though the repository is public. Add a licence before accepting outside
contributions or reuse.
