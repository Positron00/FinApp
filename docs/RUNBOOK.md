# PayRun operations runbook

For operators on call. Each alert has its own runbook in [`docs/runbooks/`](runbooks/); this page
is the index plus the everyday operations. Commands assume the repository root and a `.env`
written by `scripts/dev-env.sh`. Never paste secrets, tokens, envelopes or amounts into tickets.

## Start, stop and reset the local stack

`scripts/stack.sh` wraps Docker Compose (`deploy/compose/compose.yaml`, plus
`deploy/compose/compose.test.yaml` when the `test` profile is on). It runs `scripts/dev-env.sh`
first if `.env` is missing.

| Task                                     | Command                                                     |
| ---------------------------------------- | ----------------------------------------------------------- |
| Start everything (core, obs, test) and wait until healthy | `scripts/stack.sh up`                          |
| Start only some profiles                 | `scripts/stack.sh up --profiles core,obs`                   |
| Start and fail if a service took > 20 s to become ready (PAY-NFR-15) | `scripts/stack.sh up --check-startup` |
| Container status                         | `scripts/stack.sh ps`                                       |
| Logs with timestamps                     | `scripts/stack.sh logs [service ...]` (e.g. `payroll payout-worker`) |
| Stop everything, keep data               | `scripts/stack.sh down`                                     |
| Stop and delete volumes (all data)       | `scripts/stack.sh down -v`                                  |
| Reset: `down -v`, then `up`              | `scripts/stack.sh reset [--profiles ...]`                   |
| Seed the dev data                        | `scripts/stack.sh seed` (see [Seeding](#seeding))           |
| psql as a role                           | `scripts/stack.sh psql <role>`: `postgres`, `identity_app`, `identity_migrator`, `payroll_app`, `payroll_migrator`, `payouts_app`, `payouts_migrator`, `bank_stub_app`, `bank_stub_migrator` |

`up` builds the images, runs the one-shots in order (Postgres bootstrap SQL from
`deploy/compose/init/`, `migrate-identity`, `migrate-payroll`, `migrate-payouts`,
`migrate-bank-stub`, then `bootstrap-owner`), starts the services and writes per-service startup
times to `ci-logs/startup.json`. If the stack doesn't become healthy within
`STACK_WAIT_TIMEOUT` (300 s), it prints the status and the last 100 log lines of each failing
container.

Compose service names: `postgres`, `redis`, `mailpit`, `identity`, `payroll`, `payout-worker`,
`bank-stub`, `web`, `otel-collector`, `tempo`, `loki`, `prometheus`, `alertmanager`, `alert-sink`,
`grafana`, `postgres-exporter`, `redis-exporter`, `toxiproxy`.

## Health and readiness

| Component                 | Liveness            | Readiness                                         | Host URL (dev)                         |
| ------------------------- | ------------------- | ------------------------------------------------- | -------------------------------------- |
| web                       | `GET /api/livez`    | `GET /api/readyz`                                 | `http://127.0.0.1:4100/api/readyz`     |
| identity                  | `GET /livez`        | `GET /readyz` (DB, Redis, migrations at head)     | `http://127.0.0.1:4110/readyz` (test profile) |
| payroll                   | `GET /livez`        | `GET /readyz` (DB, Redis, migrations at head)     | `http://127.0.0.1:4111/readyz` (test profile) |
| payout-worker             | `GET /livez`        | `GET /readyz`                                     | `http://127.0.0.1:4112/readyz` (test profile) |
| bank-stub                 | `GET /livez`        | `GET /readyz` (DB and migrations only, spec R6)   | `http://127.0.0.1:4113/readyz` (test profile) |
| Grafana                   |                     | `GET /api/health`                                 | `http://127.0.0.1:4101/api/health`     |
| Prometheus / Alertmanager |                     | `GET /-/ready`                                    | `:4132/-/ready`, `:4133/-/ready`       |
| alert-sink                |                     | `GET /healthz`                                    | `http://127.0.0.1:4135/healthz`        |

Python services listen on 8000 inside their containers and the web on 3000; Compose healthchecks
and Kubernetes probes use the same paths. `/livez` does no I/O; `/readyz` runs every check
concurrently under a timeout (`libs/pycommon/src/pycommon/health.py`). A service that fails a
startup check exits with code 2 and logs `startup.check_failed` (web: `config.invalid`), naming
the setting but never its value (spec C29).

## Dashboards

Grafana at <http://127.0.0.1:4101> (user `admin`, password `GRAFANA_ADMIN_PASSWORD` in `.env`).
Files are in `deploy/observability/grafana/dashboards/`.

| Dashboard          | URL path                         | Use it for                                                    |
| ------------------ | -------------------------------- | ------------------------------------------------------------- |
| Service overview   | `/d/payrun-service-overview`     | Rate, errors and latency per service and route; SLO burn      |
| Journeys           | `/d/payrun-journeys`             | BFF journey latency, sign-ins, run throughput, settlement     |
| Queues and workers | `/d/payrun-queues`               | Stream length, consumer lag, DLQ, needs-operator, stuck payouts, outbox |
| Bank (stub) dependency | `/d/payrun-bank`             | Bank calls by outcome, breaker state, payroll calls from the worker |
| Platform           | `/d/payrun-platform`             | Postgres, Redis memory, Collector throughput and drops        |

Prometheus is at <http://127.0.0.1:4132>, Alertmanager at <http://127.0.0.1:4133>. Traces (Tempo)
and logs (Loki) are browsed through Grafana Explore; logs are JSON with an `event` field, e.g.
`{service_name="payroll"} | event="payout.needs_operator"`.

## Alerts and runbooks

Rules: `deploy/observability/prometheus/rules/alerts.yml` (17 alerts) and the Kubernetes-only
`deploy/k8s/observability/alerts-kubernetes.yml` (`PodRestarts`, spec R28). Routing
(`deploy/observability/alertmanager.yml`): `severity="page"` → receiver `page`
(`ALERT_PAGE_WEBHOOK_URL`); `severity=~"ticket|warning"` → receiver `ticket`
(`ALERT_TICKET_WEBHOOK_URL`). In dev and CI both point at alert-sink (`/page` and `/ticket`).
`BffAvailabilityFastBurn` inhibits `BffAvailabilitySlowBurn`.

| Alert                     | Severity | Route   | Service        | `for` | Runbook                                                       |
| ------------------------- | -------- | ------- | -------------- | ----- | ------------------------------------------------------------- |
| BffAvailabilityFastBurn   | page     | page    | web            | 2m    | [BffAvailabilityFastBurn](runbooks/BffAvailabilityFastBurn.md) |
| PayoutSettlementSlow      | page     | page    | payroll        | 2m    | [PayoutSettlementSlow](runbooks/PayoutSettlementSlow.md)       |
| PayoutDLQ                 | page     | page    | payout-worker  | —     | [PayoutDLQ](runbooks/PayoutDLQ.md)                             |
| PayoutNeedsOperator       | page     | page    | payroll        | 1m    | [PayoutNeedsOperator](runbooks/PayoutNeedsOperator.md)         |
| PayoutLateSettlement      | page     | page    | payroll        | —     | [PayoutLateSettlement](runbooks/PayoutLateSettlement.md)       |
| PayoutsStuck              | page     | page    | payroll        | 10m   | [PayoutsStuck](runbooks/PayoutsStuck.md)                       |
| BffAvailabilitySlowBurn   | ticket   | ticket  | web            | 15m   | [BffAvailabilitySlowBurn](runbooks/BffAvailabilitySlowBurn.md) |
| PayoutStreamNearMaxlen    | warning  | ticket  | redis          | 5m    | [PayoutStreamNearMaxlen](runbooks/PayoutStreamNearMaxlen.md)   |
| EmailOutboxStuck          | warning  | ticket  | identity       | 5m    | [EmailOutboxStuck](runbooks/EmailOutboxStuck.md)               |
| RateLimiterErrors         | warning  | ticket  | any            | 5m    | [RateLimiterErrors](runbooks/RateLimiterErrors.md)             |
| OutboxStuck               | warning  | ticket  | any            | 1m    | [OutboxStuck](runbooks/OutboxStuck.md)                         |
| ConsumerLagGrowing        | warning  | ticket  | payout-worker  | 10m   | [ConsumerLagGrowing](runbooks/ConsumerLagGrowing.md)           |
| BankBreakerOpen           | warning  | ticket  | payout-worker  | 5m    | [BankBreakerOpen](runbooks/BankBreakerOpen.md)                 |
| BankErrorRateHigh         | warning  | ticket  | payout-worker  | 10m   | [BankErrorRateHigh](runbooks/BankErrorRateHigh.md)             |
| SigninFailuresSpike       | warning  | ticket  | identity       | —     | [SigninFailuresSpike](runbooks/SigninFailuresSpike.md)         |
| RedisMemoryHigh           | warning  | ticket  | redis          | 5m    | [RedisMemoryHigh](runbooks/RedisMemoryHigh.md)                 |
| CollectorDropping         | warning  | ticket  | otel-collector | 10m   | [CollectorDropping](runbooks/CollectorDropping.md)             |
| PodRestarts (Kubernetes)  | warning  | ticket  | any            | —     | [PodRestarts](runbooks/PodRestarts.md)                         |

Each rule's `runbook_url` annotation points at the same file on GitHub, and `dashboard_url` at the
dashboard to open first. The rules have promtool tests in
`deploy/observability/prometheus/tests/` and `deploy/k8s/observability/alerts-kubernetes_test.yml`.

## Common operations

### Resolve a `needs_operator` payout

Follow [PayoutNeedsOperator](runbooks/PayoutNeedsOperator.md). In short:

1. Find the attempts: `{service_name="payroll"} | event="payout.needs_operator"` in Loki, or the
   run page in the web app (blocked payouts).
2. **Ask the bank first** (spec R8). The bank key is `<payout_id>:<attempt>`:

   ```sh
   set -a; . ./.env; set +a     # loads BANK_STUB_API_KEY and BANK_STUB_PORT; never echo them
   curl -fsS -H "X-Api-Key: $BANK_STUB_API_KEY" \
     "http://127.0.0.1:$BANK_STUB_PORT/v1/transfers/<payout_id>:<attempt>"
   ```

   After `key_window_expired`, "not found" is not proof: reconcile against the bank statement
   and put the statement reference in the note (spec C73).
3. Resolve as an approver: run page → payout → **Resolve**, or
   `POST /api/pay-runs/{id}/payouts/{payoutId}/resolve` with
   `{"action": "redrive" | "mark_rejected", "note": "..."}`. `mark_rejected` is refused with 409
   `resolve_too_early` until 120 s after the attempt entered `needs_operator` (spec C48);
   `redrive` of a `key_window_expired` attempt is refused (spec C73).

### Replay or purge the DLQ

Follow [PayoutDLQ](runbooks/PayoutDLQ.md). Read the entries as the Redis admin user:

```sh
set -a; . ./.env; set +a
docker compose --env-file .env -f deploy/compose/compose.yaml exec -e REDISCLI_AUTH="$REDIS_ADMIN_PASSWORD" \
  redis redis-cli XRANGE stream:payout.requested.dlq - + COUNT 20
```

- `error_kind = payroll` or `internal`: fix the cause; payroll re-publishes every non-final
  attempt after 360 s anyway. To replay sooner, `XADD` the stored envelope back to
  `stream:payout.requested` and `XDEL` the DLQ entry (exact commands in the runbook).
- `error_kind = invalid` (poison): never replay; resolve the attempt as above.
- Purge an entry only once its payout is final or resolved.

### Migrations

- **Compose:** `scripts/stack.sh up` runs the `migrate-<schema>` one-shots (`alembic upgrade head`
  as `<schema>_migrator`) before the services start. To re-run one by hand:
  `docker compose --env-file .env -f deploy/compose/compose.yaml --profile core up migrate-payroll`.
- **Kubernetes:** versioned Jobs `migrate-<svc>-<version>` (`deploy/k8s/base/migrations/`).
  Deploy order: `db-bootstrap` (first time, or to rotate DB passwords) → `migrate-*` (wait for
  completion) → services → web → `bootstrap-owner` (first time only). Migrations are expand-only,
  so a rollback is the previous image tags (platform LLD §9.3).
- Each service's `/readyz` fails until its schema is at head.

### Seeding

Dev only (`seed.py` refuses unless `ENV=dev`); real environments use `bootstrap_owner`.

| Command                                 | Result                                                                    |
| --------------------------------------- | ------------------------------------------------------------------------- |
| `scripts/stack.sh seed`                 | Full seed: settings (tax 2000 bps, retirement 500 bps, dual approval on), approver and load users, 10 employees on both schedules, a calculated current-month run and a draft next-month run |
| `scripts/stack.sh seed --employees 500` | The full set plus monthly employees up to 500 (k6)                         |
| `scripts/stack.sh seed --skeleton`      | Owner, approver, load user, 5 salaried monthly employees                  |
| `... --out ci-logs/seed.json`           | Also writes the JSON summary (used by Playwright, k6 and Schemathesis)    |

A re-run is a no-op that prints the same IDs. Exit codes: 1 an API step failed, 2 arguments or
`ENV`, 3 the stack wasn't ready within 60 s.

### Key rotation

Documented procedures (none is automated in 0.1.0):

- **Bank-field encryption keys** (payroll LLD §3.6): add the new kid to `PAYROLL_FIELD_KEYS` and
  point `PAYROLL_FIELD_ACTIVE_KID` at it. There is no re-key pass: keep every old kid while any
  ciphertext uses it, or approve and the instruction fail with 500 `internal_error`.
- **Internal JWT signing keys** (web LLD §3.4.1): add the new public key to every service's
  `INTERNAL_JWT_PUBLIC_KEYS` first, then switch `INTERNAL_JWT_PRIVATE_KEY` and
  `INTERNAL_JWT_KID`, then remove the old key after 60 s. The worker key
  (`PAYOUT_WORKER_JWT_*`, trusted by payroll only) follows the same order. uat and prod refuse
  kids starting `dev-` or `test-`.
- **Database role passwords:** change the secret and re-run the `db-bootstrap` Job, which
  re-applies `ALTER ROLE … PASSWORD` (platform LLD §2.1, §9.3). In Compose the bootstrap SQL runs
  only when the Postgres volume is created, so a dev rotation is `scripts/stack.sh reset`.
- **`BANK_STUB_API_KEY`:** the worker and bank-stub must change together; a mismatch shows as
  `client_error` bank calls ([BankErrorRateHigh](runbooks/BankErrorRateHigh.md)).

### Mint a token for manual API calls (dev only)

```sh
uv run python scripts/mint_token.py --aud payroll --sub <user-uuid> --roles approver
```

It prints a 60 s token on stdout; it refuses unless `ENV=dev`. Use it with
`curl -H "Authorization: Bearer ..." http://127.0.0.1:4111/v1/...` on the test profile.

## Secrets per environment

From plan §8 and [ADR 0008](adr/0008-secrets.md). Values are never committed; `.env.example`
lists every dev key with empty secrets.

| Secret                                                                  | dev / CI                         | uat / prod                                   |
| ----------------------------------------------------------------------- | -------------------------------- | -------------------------------------------- |
| `POSTGRES_PASSWORD`                                                     | `.env` (`scripts/dev-env.sh`)    | Managed DB admin, via External Secrets       |
| `{IDENTITY,PAYROLL,PAYOUTS}_DB_MIGRATOR_PASSWORD`, `..._DB_PASSWORD`    | `.env`                           | External Secrets                             |
| `BANK_STUB_DB_MIGRATOR_PASSWORD`, `BANK_STUB_DB_PASSWORD`               | `.env`                           | External Secrets (uat only; no bank-stub in prod) |
| `REDIS_{ADMIN,WEB,IDENTITY,PAYROLL,PAYOUT_WORKER}_PASSWORD`             | `.env`                           | External Secrets (managed Redis ACLs)        |
| `INTERNAL_JWT_PRIVATE_KEY`, `INTERNAL_JWT_KID`                          | `.env`                           | External Secrets                             |
| `PAYOUT_WORKER_JWT_PRIVATE_KEY`, `PAYOUT_WORKER_JWT_KID`                | `.env`                           | External Secrets                             |
| `INTERNAL_JWT_PUBLIC_KEYS` (one keyset per service)                     | derived in `.env`                | ConfigMap per service (public keys)          |
| `PAYROLL_FIELD_KEYS`, `PAYROLL_FIELD_ACTIVE_KID`                        | `.env`                           | External Secrets; `dev-` kids refused        |
| `SMTP_HOST`, `SMTP_USERNAME`, `SMTP_PASSWORD`                           | Mailpit, no credentials          | External Secrets                             |
| `ALERT_PAGE_WEBHOOK_URL`, `ALERT_TICKET_WEBHOOK_URL`                    | alert-sink                       | External Secrets (providers to be chosen, [ADR 0010](adr/0010-alert-receivers.md)) |
| `GRAFANA_ADMIN_PASSWORD`                                                | `.env`                           | External Secrets, or the hosted provider's auth |
| `POSTGRES_EXPORTER_PASSWORD`, `REDIS_EXPORTER_PASSWORD`                 | `.env`                           | External Secrets                             |
| `AUDIT_IP_HASH_KEY`, `SESSION_SECRET`, `BANK_STUB_API_KEY`              | `.env`                           | External Secrets                             |
| `SEED_OWNER_PASSWORD`, `SEED_APPROVER_PASSWORD`, `LOAD_USER_PASSWORD`   | `.env`                           | not present                                  |

For the Kubernetes dev overlay, `scripts/dev-env.sh --k8s-dev` writes the git-ignored
`deploy/k8s/overlays/dev/secrets.env`. The uat and prod overlays use `ExternalSecret` resources
against a `ClusterSecretStore` named `payrun-secrets`; every `REPLACE` in them is a placeholder for
the environment's owner.

## Alert drills

`scripts/ci/alert_drills.sh` proves that each paging alert fires, reaches alert-sink `/page` with
its runbook link, shows its cause on the dashboard, and clears. It isn't run in CI: it stops
containers and changes Toxiproxy.

```sh
scripts/stack.sh up && scripts/stack.sh seed
scripts/ci/alert_drills.sh                         # all six paging alerts, one after another
scripts/ci/alert_drills.sh PayoutDLQ PayoutNeedsOperator   # a subset
```

It causes PayoutDLQ (a poison stream entry), PayoutNeedsOperator and PayoutLateSettlement (worker
stopped, forced results via `mint_token`), PayoutSettlementSlow (bank proxy disabled),
PayoutsStuck (worker stopped; about 22 minutes by design) and BffAvailabilityFastBurn (identity
stopped). Every fault is undone on exit. Timeouts are tunable with `DRILL_TIMEOUT_S`,
`DRILL_STUCK_TIMEOUT_S`, `DRILL_QUIET_WAIT_S`, `DRILL_RECOVERY_WAIT_S`, `DRILL_POLL_S` and
`DRILL_LOAD_WORKERS`. The 2026-11-11 results are in spec C75.

To see what alert-sink received: `curl -fsS http://127.0.0.1:4135/page` (or `/ticket`);
`curl -X DELETE http://127.0.0.1:4135/all` clears it.

## Client IP and trusted proxy hops

**From the security review (INFO):** the BFF derives the client IP from `X-Forwarded-For` and
`TRUSTED_PROXY_HOPS` (`apps/web/src/lib/client-ip.ts`, spec C62). The client is entry
`n − TRUSTED_PROXY_HOPS`. The uat and prod overlays set `TRUSTED_PROXY_HOPS=1`
(`deploy/k8s/overlays/{uat,prod}/patches/config.yaml`), which assumes exactly one trusted proxy:
ingress-nginx, replacing `X-Forwarded-For` with the address it saw. uat and prod refuse a value
below 1 at startup.

If you put another L7 load balancer in front of ingress-nginx, **raise `TRUSTED_PROXY_HOPS`** to
the number of trusted proxies (the code assumes each one appends the address it saw). Otherwise every user is seen as the
load balancer's IP and shares one IP's rate limits (BFF 300/min per IP, sign-in 20 per 15 min per
IP), and the auth audit's IP hashes are useless. Dev and Compose use `TRUSTED_PROXY_HOPS=0`.
