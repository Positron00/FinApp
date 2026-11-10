# Low-level design: bank-stub

Status: draft 2026-11-10 · Sources: [architecture plan](../../plan/architecture-plan.md) v1.2 (§2.2, §4, §6, §7, §8, §9, §12.1, §13), [ADR 0001](../../adr/0001-service-map.md), [ADR 0003](../../adr/0003-payout-flow.md) (a, e), [ADR 0011](../../adr/0011-egress.md), [requirements](requirements.md) (PAY-412, PAY-605, PAY-606, PAY-NFR-02, PAY-NFR-07), [high-level design](design-high-level.md), [payout-worker low-level design](design-low-level-payout-worker.md) (the only caller).

bank-stub simulates a payment processor. It has an idempotent, persistent ledger: one row per
`Idempotency-Key` in `bank_stub.transfers`, written with `INSERT … ON CONFLICT DO NOTHING
RETURNING`. That makes a repeated key return the original transfer across restarts, replicas and
concurrent POSTs (ADR 0003 a). Its outcome is deterministic per key, so tests can repeat it. It
runs in the Compose `core` profile and in the dev and uat Kubernetes overlays, **never in prod**
(ADR 0001). Only payout-worker may reach it (ADR 0011). It doesn't use Redis, the internal JWT or
the outbox.

## 1. Modules

All paths are under `services/bank_stub/`. The package is `bank_stub` (Compose and Kubernetes name: `bank-stub`).

| Module (path) | New / changed | Responsibility |
|---|---|---|
| `src/bank_stub/main.py` | New | `create_app(settings) -> FastAPI` via `pycommon.create_app(settings, routers=[transfers.router], internal_auth=False, redis=False)`: OTel, request logging, the 256 KB body limit, error handlers, `/livez` and `/readyz` (DB and migrations only). The lifespan runs the startup checks and opens the DB engine. `app = create_app(Settings())` for uvicorn. |
| `src/bank_stub/settings.py` | New | `Settings` (pydantic-settings; names exactly as §4) and `startup_checks(settings)` (§3.5). |
| `src/bank_stub/api/auth.py` | New | `require_api_key` dependency: constant-time check of `X-Api-Key` (§3.3). |
| `src/bank_stub/api/transfers.py` | New | `POST /v1/transfers` and `GET /v1/transfers/{key}` (§3.1, §3.2). HTTP mapping only. |
| `src/bank_stub/schemas.py` | New | `TransferRequest`, `TransferResponse`, and the key format (§2.3). |
| `src/bank_stub/domain/outcome.py` | New | `decide_outcome(key, account, failure_rate)` and `bank_reference_for(key)` (§3.4). Pure functions. |
| `src/bank_stub/db/models.py` | New | SQLAlchemy Core table `bank_stub.transfers` (§2.1). |
| `src/bank_stub/db/repo.py` | New | `TransferRepo.insert_or_get(...)` and `get_by_key(key)` (§3.1). |
| `src/bank_stub/metrics.py` | New | `bank_stub_transfers_total` (§5.2). |
| `migrations/` (`env.py`, `versions/0001_bank_stub_transfers.py`) | New | Alembic; version table `bank_stub.alembic_version` (§2.2). |
| `openapi.json` | New (generated) | Exported by `scripts/export_openapi.py`; drift-checked in CI. |
| `tests/{unit,integration,contract}/` | New | §6. |
| `Dockerfile` | New | Non-root, read-only root filesystem, `ENV OTEL_SEMCONV_STABILITY_OPT_IN=http`. |

## 2. Data models

### 2.1 Schema `bank_stub`

Owned by the migrator role `bank_stub_migrator` (`BANK_STUB_DB_MIGRATOR_PASSWORD`); the runtime role
`bank_stub_app` (`BANK_STUB_DB_PASSWORD`) has `USAGE` on the schema and DML on its tables through the
default privileges from `deploy/compose/init/*.sql` (PAY-606). Only the last 4 digits of the account
are stored; the routing number isn't stored at all (plan §6).

```sql
CREATE TABLE bank_stub.transfers (
  id               uuid        PRIMARY KEY,               -- transfer_id, UUIDv7, set by the app
  idempotency_key  text        NOT NULL
                     CHECK (idempotency_key ~ '^[A-Za-z0-9:_-]{1,255}$'),
  amount_cents     bigint      NOT NULL CHECK (amount_cents > 0 AND amount_cents <= 1000000000),
  currency         char(3)     NOT NULL CHECK (currency = 'USD'),
  account_last4    char(4)     NOT NULL CHECK (account_last4 ~ '^[0-9]{4}$'),
  status           text        NOT NULL CHECK (status IN ('settled', 'rejected')),
  reason           text        NULL CHECK (reason IN ('account_closed', 'insufficient_funds_at_bank')),
  bank_reference   text        NULL CHECK (bank_reference ~ '^BSR-[0-9A-F]{16}$'),
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT transfers_idempotency_key_uq UNIQUE (idempotency_key),
  CONSTRAINT transfers_outcome_shape CHECK (
    (status = 'settled'  AND reason IS NULL     AND bank_reference IS NOT NULL) OR
    (status = 'rejected' AND reason IS NOT NULL AND bank_reference IS NULL))
);
```

Rows are never updated after insert (`updated_at` = `created_at`). Retention: dev and uat only;
truncated by `scripts/stack.sh reset` (plan §6).

### 2.2 Migration

`services/bank_stub/migrations/versions/0001_bank_stub_transfers.py`, revision `0001_bank_stub_transfers`, `down_revision = None`.
- `env.py` sets `version_table_schema="bank_stub"` and connects with `MIGRATOR_DATABASE_URL` (role `bank_stub_migrator`). The schema and roles come from the bootstrap SQL.
- `upgrade()`: creates `bank_stub.transfers` exactly as §2.1. The `UNIQUE` constraint provides the index on `idempotency_key`.
- `downgrade()`: drops the table.
- Runs as the Compose one-shot `migrate-bank-stub` and the Kubernetes Job `migrate-bank-stub-<version>` (dev and uat overlays only).

### 2.3 API models (`schemas.py`)

```python
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9:_-]{1,255}$"   # the worker sends "<payout_id>:<attempt>"

class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_cents: int = Field(gt=0, le=1_000_000_000)
    currency: Literal["USD"]
    routing: str = Field(pattern=r"^\d{9}$")
    account: str = Field(pattern=r"^\d{4,17}$")
    # __repr__ masks routing and account; never logged

class TransferResponse(BaseModel):
    transfer_id: UUID
    idempotency_key: str
    status: Literal["settled", "rejected"]
    reason: Literal["account_closed", "insufficient_funds_at_bank"] | None
    bank_reference: str | None          # "BSR-" + 16 uppercase hex, settled only
    amount_cents: int
    currency: Literal["USD"]
    account_last4: str
    created_at: AwareDatetime
```

Errors use the shared format `{error: {code, message, request_id, details?}}` (PAY-602).

## 3. Interfaces

### 3.1 `POST /v1/transfers` (PAY-412)

Request headers: `X-Api-Key` (required), `Idempotency-Key` (required), `Content-Type: application/json`. Body: `TransferRequest`.

Steps, in this order:
1. `require_api_key` (§3.3). Failure → 401 `invalid_api_key`.
2. `Idempotency-Key` missing → 422 `idempotency_key_required`. Not matching `IDEMPOTENCY_KEY_PATTERN` → 422 `idempotency_key_invalid`.
3. `await asyncio.sleep(BANK_STUB_LATENCY_MS / 1000)` (§3.6).
4. **Replay lookup:** `row = await repo.get_by_key(key)`. If found → **200** with that row, **whatever the body** (the body isn't validated or compared). Log `bank_stub.transfer.replayed`; metric outcome `replayed`.
5. Validate the body with `TransferRequest.model_validate_json(raw)`. Failure → 422 `validation_error` with field names in `error.details` (values never echoed).
6. `outcome = decide_outcome(key, body.account, BANK_STUB_FAILURE_RATE)`; `bank_reference = bank_reference_for(key)` when settled.
7. `row, created = await repo.insert_or_get(...)` in one transaction:

   ```sql
   INSERT INTO bank_stub.transfers
     (id, idempotency_key, amount_cents, currency, account_last4, status, reason, bank_reference)
   VALUES (:id, :key, :amount_cents, :currency, :last4, :status, :reason, :bank_reference)
   ON CONFLICT (idempotency_key) DO NOTHING
   RETURNING *;
   -- no row returned → a concurrent request inserted this key first:
   SELECT * FROM bank_stub.transfers WHERE idempotency_key = :key;
   ```

   Under READ COMMITTED, a second concurrent INSERT with the same key waits for the first
   transaction, then does nothing. The following SELECT is a new statement, so it sees the
   committed row. Exactly one row per key; every concurrent caller gets the same `transfer_id`.
8. `created` → **201** (log `bank_stub.transfer.created`, metric outcome `settled` or `rejected`); otherwise → **200** (as step 4).

The body is read raw and validated in the handler (not by FastAPI before the handler), so step 4
can serve replays whatever the body. The OpenAPI document still declares the `TransferRequest`
request body via `openapi_extra`.

| Status | Code | When |
|---|---|---|
| 201 | — | New transfer (settled or rejected: a rejection is still 2xx) |
| 200 | — | Repeated key: the stored row |
| 401 | `invalid_api_key` | Missing or wrong `X-Api-Key` |
| 413 | `payload_too_large` | Body over 256 KB |
| 422 | `idempotency_key_required`, `idempotency_key_invalid`, `validation_error` | As above |
| 500 | `internal_error` | Unhandled |
| 503 | `db_timeout`, `db_unavailable` | Statement or pool timeout; DB down |

The worker classifies only a 2xx with `status: rejected` and a `reason` as a definite rejection;
every other status here is "no answer" to it (ADR 0003 e).

### 3.2 `GET /v1/transfers/{key}` (PAY-412)

Headers: `X-Api-Key` (required). Path `key` must match `IDEMPOTENCY_KEY_PATTERN` (the `:` in the worker's key is a valid path character).
1. `require_api_key` → 401 `invalid_api_key`.
2. Invalid key format → 422 `idempotency_key_invalid`.
3. Sleep `BANK_STUB_LATENCY_MS`.
4. `get_by_key(key)` → 200 `TransferResponse`, or 404 `transfer_not_found`.

This is the provider-adapter lookup: the worker uses it after the key safe window (PAY-410), and tests and operators use it to reconcile.

### 3.3 API key check (`api/auth.py`)

```python
async def require_api_key(x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
                          settings: Settings = Depends(get_settings)) -> None:
    if x_api_key is None or not hmac.compare_digest(
            x_api_key.encode(), settings.bank_stub_api_key.get_secret_value().encode()):
        raise ApiError(401, "invalid_api_key", "API key missing or invalid.")
```

- It applies to both `/v1/transfers` routes, not to `/livez` or `/readyz`.
- A failure logs `bank_stub.auth.rejected` with `reason` (`missing` or `mismatch`), never the key.
- The redactor masks `x-api-key` and `api_key` in request logs and spans.

### 3.4 Outcome function (`domain/outcome.py`)

```python
Outcome = tuple[Literal["settled", "rejected"], Literal["account_closed", "insufficient_funds_at_bank"] | None]

def decide_outcome(key: str, account: str, failure_rate: float) -> Outcome:
    if account.endswith("0000"):
        return ("rejected", "account_closed")
    bucket = int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest(), "big") % 10_000
    if bucket < round(failure_rate * 10_000):
        return ("rejected", "insufficient_funds_at_bank")
    return ("settled", None)

def bank_reference_for(key: str) -> str:
    return "BSR-" + hashlib.sha256(b"ref:" + key.encode("utf-8")).hexdigest()[:16].upper()
```

- The account rule is checked first. `account_last4 == "0000"` ⇔ `account.endswith("0000")`.
- The failure check is integer arithmetic on the key only. The same key always gives the same outcome, whatever the replica, restart or body, so a retry with a new attempt (new key) can get a different outcome.
- The threshold is `round(BANK_STUB_FAILURE_RATE × 10000)`: 0.05 gives 500, so buckets 0–499 are rejected. Rate 0 never rejects; rate 1 always does.
- The reference format is `BSR-` plus 16 uppercase hex characters, derived from the key (deterministic; stored anyway).
- `transfer_id` is a fresh UUIDv7 per inserted row.

### 3.5 Health and startup checks (PAY-605)

- `/livez`: 200 while the process is up.
- `/readyz`: 200 when the DB answers `SELECT 1` and `bank_stub.alembic_version` is at head; otherwise 503. bank-stub has no Redis, so there is no Redis check.
- `startup_checks(settings)` logs `bank_stub.startup.refused` with the setting's name (never the value) and exits with code 1 when:
  - `ENV=prod` (bank-stub is never deployed in prod);
  - `BANK_STUB_FAILURE_RATE` isn't in [0, 1], or `BANK_STUB_LATENCY_MS` isn't in [0, 10000];
  - in uat: `BANK_STUB_API_KEY` or `BANK_STUB_DB_PASSWORD` is missing or a dev default, or debug or docs are on.

### 3.6 Latency

Every `/v1/transfers` request (POST and GET, after the API-key and key-format checks) awaits
`asyncio.sleep(BANK_STUB_LATENCY_MS / 1000)` before touching the database. The default is 20 ms
(plan §2.2). Larger delays and faults (timeouts, resets, outages) come from Toxiproxy in the `test`
profile, not from bank-stub settings.

## 4. Configuration

| Key / env var | Type | Default (dev / uat / prod) | Requirement |
|---|---|---|---|
| `ENV` | `dev\|uat` | dev / uat / not deployed (refused) | PAY-605, ADR 0001 |
| `DATABASE_URL` | URL | `postgresql+asyncpg://bank_stub_app:${BANK_STUB_DB_PASSWORD}@postgres:5432/payrun` / External Secrets / — | PAY-606 |
| `MIGRATOR_DATABASE_URL` (migrate job only) | URL | as above with `bank_stub_migrator:${BANK_STUB_DB_MIGRATOR_PASSWORD}` / External Secrets / — | PAY-606 |
| `DB_STATEMENT_TIMEOUT_MS` / `DB_POOL_TIMEOUT_S` | int / float | 2000 / 2 | plan §2.2 |
| `DB_POOL_SIZE` | int | 20 | PAY-412 (concurrent POSTs) |
| `BANK_STUB_API_KEY` | secret | `.env` (`dev-env.sh`) / External Secrets / — | PAY-412 |
| `BANK_STUB_FAILURE_RATE` | float [0, 1] | 0.05 / 0.05 / — | PAY-412 |
| `BANK_STUB_LATENCY_MS` | int [0, 10000] | 20 / 20 / — | PAY-412 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | URL | `http://otel-collector:4317` | PAY-NFR-09 |
| `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` | str / float | `parentbased_traceidratio` / 1.0 / 1.0 | plan §2.2 |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | str | `http` (Dockerfile) | observability |

Port 8000 in the container; host 4113 in the Compose test override (plan §13).

## 5. Logging and metrics

Logs are JSON through `pycommon.logging`, with `trace_id`. The worker's `traceparent` header makes
bank-stub's server span part of the J4 trace (PAY-NFR-09). Logs and spans never contain the API
key, routing, the full account or the amount (PAY-NFR-07). The idempotency key (`<payout_id>:<attempt>`)
and `transfer_id` are IDs, not personal data, and are logged.

### 5.1 Log events

| Event | Level | Fields | When | Requirement |
|---|---|---|---|---|
| `bank_stub.transfer.created` | info | `transfer_id`, `idempotency_key`, `status`, `reason` | New row inserted (201) | PAY-412 |
| `bank_stub.transfer.replayed` | info | `transfer_id`, `idempotency_key`, `status` | Repeated key (200), including a lost concurrent insert | PAY-412 |
| `bank_stub.transfer.not_found` | info | `idempotency_key` | GET 404 | PAY-412 |
| `bank_stub.auth.rejected` | warning | `reason` (missing, mismatch), `path` | 401 | PAY-412 |
| `bank_stub.startup.refused` | critical | `setting` | Startup check failed | PAY-605 |
| `http.request` | info | pycommon standard (route, status, duration) | Every request | plan §11 |

### 5.2 Metrics

| Metric | Type | Labels (values) | When | Requirement |
|---|---|---|---|---|
| `bank_stub_transfers_total` | counter | `outcome` (settled, rejected, replayed) | Once per POST that returns 200/201 | PAY-412 |
| `http_server_request_duration_seconds` | histogram | `http_route`, `http_response_status_code` (auto) | Every request | plan §11 |

Rejection reasons aren't a label here: the worker's `payouts_bank_transfers_total{outcome, reason}` already carries them.

## 6. Tests

Unit tests have no I/O. Integration tests use a session-scoped Testcontainers `postgres:16-alpine`.
The fixture runs `deploy/compose/init/*.sql`, migrates as `bank_stub_migrator`, connects as
`bank_stub_app`, and truncates `bank_stub.transfers` after each test. The app is driven through
`httpx.AsyncClient(transport=ASGITransport(app))`.

| Test file | Covers | Requirement |
|---|---|---|
| `tests/unit/test_outcome.py` | Account ending `0000` → `account_closed` (even at rate 0); the same key always gives the same outcome; rate 0 never rejects, rate 1 always does; boundary: a key with bucket 499 is rejected at 0.05 and a key with bucket 500 isn't; over 10,000 random keys at 0.05 the rejected share is 5% ± 1%; reference format `^BSR-[0-9A-F]{16}$` | PAY-412 |
| `tests/unit/test_schemas.py` | `TransferRequest` bounds and patterns, `extra="forbid"`; `repr` masks routing and account; key pattern accepts `<uuid>:<n>` | PAY-412, NFR-07 |
| `tests/unit/test_api_key.py` | Missing → 401; wrong → 401; correct → passes; uses `hmac.compare_digest` | PAY-412 |
| `tests/unit/test_latency.py` | The handler awaits `sleep(BANK_STUB_LATENCY_MS / 1000)` (patched sleep) on POST and GET | PAY-412 |
| `tests/unit/test_startup_checks.py` | `ENV=prod` refused; rate and latency bounds; uat dev-default key refused; the log names the setting, never the value | PAY-605 |
| `tests/integration/test_migrations.py` | Upgrade to head, downgrade one, upgrade again; constraints (unique key, outcome shape, `USD` only) | PAY-606 |
| `tests/integration/test_grants.py` | `bank_stub_app` gets `permission denied` on `payroll.*`, `payouts.*`, `identity.*` | PAY-606 |
| `tests/integration/test_transfers_api.py` | POST new → 201 settled with a reference; account `…0000` → 201 rejected `account_closed`; repeated key → 200, the same `transfer_id`, even with a different (or invalid) body; GET → 200 / 404 `transfer_not_found`; missing API key → 401 on both routes; missing `Idempotency-Key` → 422 `idempotency_key_required`; invalid body on a new key → 422 with field names | PAY-412 |
| `tests/integration/test_concurrent_same_key.py` | 50 concurrent POSTs with one key through **two app instances** (separate engines and pools, like two replicas) → exactly one row; every response has the same `transfer_id`, `status` and `bank_reference`; exactly one 201; `bank_stub_transfers_total` grows by one `settled`/`rejected` and the rest `replayed` | PAY-412, NFR-02 |
| `tests/integration/test_restart_persistence.py` | POST a key, shut down the app and dispose its engine, build a new app → GET returns the same row, and a repeated POST returns 200 with the same `transfer_id` | PAY-412, NFR-02 |
| `tests/integration/test_readyz.py` | 503 when the DB is down or migrations aren't at head; no Redis needed | PAY-605 |
| `tests/integration/test_no_secrets_in_logs.py` | Captured logs and spans for POST/GET/401 contain none of: the API key, routing, full account, amount | PAY-NFR-07 |
| `tests/integration/test_metrics.py` | In-memory reader: `bank_stub_transfers_total{outcome}` values after created, rejected and replayed calls | PAY-412 |
| `tests/contract/test_openapi.py` | `openapi.json` matches the app (drift); Schemathesis against the app finds no 5xx and no schema violations | PAY-NFR-12 |

The Compose-level restart scenario (bank-stub restarted mid-run, ≤ 1 settled transfer per payout)
is owned by the worker's resilience suite: `tests/resilience/test_bank_restart.py`.

## 7. Traceability

| Requirement | Design section | Tests |
|---|---|---|
| PAY-412: POST with `Idempotency-Key` and `X-Api-Key`, ON CONFLICT, replay 200 | §3.1, §3.3 | `test_transfers_api.py`, `test_api_key.py` |
| PAY-412: outcome rules | §3.4 | `test_outcome.py` |
| PAY-412: latency | §3.6 | `test_latency.py` |
| PAY-412: GET 200/404 | §3.2 | `test_transfers_api.py` |
| PAY-412: missing API key → 401 | §3.3 | `test_api_key.py`, `test_transfers_api.py` |
| PAY-412: concurrent POSTs → one row | §3.1 step 7 | `test_concurrent_same_key.py` |
| PAY-412: ledger survives restart | §2.1, §3.1 | `test_restart_persistence.py`, `tests/resilience/test_bank_restart.py` |
| PAY-605 | §3.5 | `test_startup_checks.py`, `test_readyz.py` |
| PAY-606 | §2.1, §2.2 | `test_migrations.py`, `test_grants.py` |
| PAY-NFR-02 (bank side) | §3.1, §3.4 | `test_concurrent_same_key.py`, `test_restart_persistence.py` |
| PAY-NFR-07 | §2.3, §5 | `test_schemas.py`, `test_no_secrets_in_logs.py` |
| PAY-NFR-09 | §5 | e2e `trace.spec.ts` |

## Lead resolutions applied (2026-11-10; README R-table)

These override anything earlier in this file that disagrees.

- **R6:** the response is `{transfer_id, status, reason, bank_reference}`: 201 when new, 200 on a replay.
- The key regex is `^[A-Za-z0-9:_-]{1,255}$`, and the lookup happens before body validation.
- `/readyz` checks only the DB.
- The metric `bank_stub_transfers_total{outcome}` is added to plan §11.
- **R13:** the extra columns are accepted.

## Issues for the lead (all resolved: see the section above and the spec README R-table)

1. **The response fields don't agree.** Plan §7 gives the POST response as `{transfer_id, status, reason}`, but PAY-404 and PAY-412 need a `bank_reference` for settled transfers. This design returns `bank_reference`, plus `idempotency_key`, `amount_cents`, `currency`, `account_last4` and `created_at` (§2.3). Plan §7 should list at least `bank_reference`.
2. **The status for a new transfer isn't stated.** PAY-412 gives 200 for a repeated key only. This design uses 201 for a newly created transfer and 200 for a replay. Both are 2xx, so the worker's classification is the same either way. Please confirm.
3. **"Whatever the body" means skipping validation on replays.** PAY-412 says a repeated key returns the stored row "whatever the body". This design looks the key up before validating the body, so even an invalid body gets the stored row (§3.1 step 4). A real provider would more likely return 422 for a mismatched body, and the worker would classify that as "no answer". The stub follows the requirement literally.
4. **PAY-605 says `/readyz` checks Redis on every service.** bank-stub has no Redis, so its `/readyz` checks only the DB and migrations.
5. **The new metric isn't in the observability lists.** `bank_stub_transfers_total{outcome}` isn't in plan §11 or the PAY-NFR-10 metric list. It should be added there, or kept as a stub-only metric outside the NFR-10 check.
6. **The schema has columns plan §6 doesn't list.** Plan §6 lists `transfers` as (idempotency_key, amount_cents, account_last4, status, reason). This design adds `id` (`transfer_id`), `currency`, `bank_reference`, `created_at` and `updated_at`. The routing number is deliberately not stored.
7. **The idempotency key format isn't specified.** No document gives an allowed character set or length. This design uses `^[A-Za-z0-9:_-]{1,255}$`, which fits `<payout_id>:<attempt>`.
