# Low-level design: bank-stub

Status: draft 2026-11-10, aligned with the README Spec resolutions (R) and Contract decisions (C) tables · Sources: [spec README](README.md) (R- and C-tables), [architecture plan](../../plan/architecture-plan.md) v1.2 (§2.2, §4, §6, §7, §8, §9, §12.1, §13), [ADR 0001](../../adr/0001-service-map.md), [ADR 0003](../../adr/0003-payout-flow.md) (a, e), [ADR 0011](../../adr/0011-egress.md), [requirements](requirements.md) (PAY-412, PAY-605, PAY-606, PAY-NFR-02, PAY-NFR-07, PAY-NFR-09), [high-level design](design-high-level.md), [payout-worker low-level design](design-low-level-payout-worker.md) (the only caller), [platform low-level design](design-low-level-platform.md) (pycommon, env).

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
| `src/bank_stub/main.py` | New | `create_app(settings) -> FastAPI` via `pycommon.app.create_app(settings, public_routers=[transfers.router], use_redis=False, use_jwt=False, migrations_script_location="migrations", startup_checks=[check_not_prod])`: OTel, request logging, the 256 KB body limit, error handlers, `/livez` and `/readyz` (DB and migrations only; R6). pycommon's lifespan runs the startup checks before opening the DB engine. `app = create_app(load_settings(Settings))`; served by `python -m pycommon.serve` with `PYCOMMON_APP=bank_stub.main:app`. |
| `src/bank_stub/settings.py` | New | `Settings(BaseServiceSettings)` (pycommon; names exactly as §4, C1) and the extra startup check `check_not_prod` (§3.5). |
| `src/bank_stub/api/auth.py` | New | `require_api_key` dependency: constant-time check of `X-Api-Key` (§3.3). |
| `src/bank_stub/api/transfers.py` | New | `POST /v1/transfers` and `GET /v1/transfers/{key}` (§3.1, §3.2). HTTP mapping only. |
| `src/bank_stub/schemas.py` | New | `TransferRequest`, `TransferResponse`, and the key format (§2.3). |
| `src/bank_stub/domain/outcome.py` | New | `decide_outcome(key, account, failure_rate)` and `bank_reference_for(key)` (§3.4). Pure functions. |
| `src/bank_stub/db/models.py` | New | SQLAlchemy Core table `bank_stub.transfers` (§2.1). |
| `src/bank_stub/db/repo.py` | New | `TransferRepo.insert_or_get(...)` and `get_by_key(key)` (§3.1). |
| `src/bank_stub/metrics.py` | New | `bank_stub_transfers_total` (§5.2). |
| `migrations/` (`env.py`, `versions/0001_bank_stub_transfers.py`) | New | Alembic via `pycommon.db.run_alembic_env(metadata, schema="bank_stub", runtime_role="bank_stub_app")`; version table `bank_stub.alembic_version` (§2.2). |
| `openapi.json` | New (generated) | Exported by `scripts/export_openapi.py`; drift-checked in CI. |
| `tests/{unit,integration,contract}/` | New | §6. |
| `Dockerfile` | New | The shared Python service template: non-root, read-only root filesystem, `ENV OTEL_SEMCONV_STABILITY_OPT_IN=http`. |

## 2. Data models

### 2.1 Schema `bank_stub`

Owned by the migrator role `bank_stub_migrator` (password env `BANK_STUB_DB_MIGRATOR_PASSWORD`); the
runtime role `bank_stub_app` (password env `BANK_STUB_DB_PASSWORD`) has `USAGE` on the schema and DML
on its tables through the default privileges from `deploy/compose/init/40-bank-stub.sql` (PAY-606,
C2). The service connects with the C1 keys (§4), never a URL. Only the last 4 digits of the account
are stored; the routing number isn't stored at all (plan §6). The columns beyond plan §6 (`id`,
`currency`, `bank_reference`, `created_at`, `updated_at`) are accepted by R13.

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
  bank_reference   text        NULL CHECK (bank_reference ~ '^BSR-[0-9A-F]{16}$'),   -- 20 chars, within the C26 limit of 64
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
- `env.py` calls `pycommon.db.run_alembic_env(metadata, schema="bank_stub", runtime_role="bank_stub_app")`: `version_table_schema="bank_stub"`, filtered to `bank_stub`. It connects with the C1 keys set to the migrator's credentials in the migrate container (`DB_USER=bank_stub_migrator`, `DB_PASSWORD=${BANK_STUB_DB_MIGRATOR_PASSWORD}`, `DB_SCHEMA=bank_stub`). After `upgrade` the helper revokes `INSERT, UPDATE, DELETE` on `bank_stub.alembic_version` from `bank_stub_app`. The schema and roles come from the bootstrap SQL.
- `upgrade()`: creates `bank_stub.transfers` exactly as §2.1. The `UNIQUE` constraint provides the index on `idempotency_key`.
- `downgrade()`: drops the table.
- Runs as the Compose one-shot `migrate-bank-stub` and the Kubernetes Job `migrate-bank-stub-<version>` (dev and uat overlays only).

### 2.3 API models (`schemas.py`)

```python
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9:_-]{1,255}$"   # R6; the worker sends "<payout_id>:<attempt>"
IDEMPOTENCY_KEY_RE = re.compile(IDEMPOTENCY_KEY_PATTERN)  # always .fullmatch(): "$" alone accepts a trailing "\n" (C63)

class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_cents: int = Field(gt=0, le=1_000_000_000)
    currency: Literal["USD"]
    routing: str = Field(pattern=r"^\d{9}$")
    account: str = Field(pattern=r"^\d{4,17}$")
    # __repr__ masks routing and account; never logged

class TransferResponse(BaseModel):                     # R6, PAY-412: exactly these four fields
    transfer_id: UUID
    status: Literal["settled", "rejected"]
    reason: Literal["account_closed", "insufficient_funds_at_bank"] | None   # rejected only
    bank_reference: str | None = Field(max_length=64)  # "BSR-" + 16 uppercase hex, settled only (C26)
```

The same body is returned by POST (201 new, 200 replay) and GET (200). The stored `amount_cents`,
`currency`, `account_last4` and timestamps stay in the ledger and aren't returned.

Errors use the shared format `{error: {code, message, request_id, details?}}` (PAY-602). For
`validation_error`, `details` is `[{field, issue}]` and never echoes the input (C14).

## 3. Interfaces

### 3.1 `POST /v1/transfers` (PAY-412)

Request headers: `X-Api-Key` (required), `Idempotency-Key` (required), `Content-Type: application/json`. Body: `TransferRequest`.

Steps, in this order:
1. `require_api_key` (§3.3). Failure → 401 `invalid_api_key`.
2. `Idempotency-Key` missing → **400 `idempotency_key_required`** (C34). Not matching `IDEMPOTENCY_KEY_PATTERN` → **422 `validation_error`** with `details=[{"field": "header.Idempotency-Key", "issue": "pattern"}]` (C34, C14).
3. `await asyncio.sleep(BANK_STUB_LATENCY_MS / 1000)` (§3.6).
4. **Replay lookup (before the body is validated; R6):** `row = await repo.get_by_key(key)`. If found → **200** with that row as `TransferResponse`, **whatever the body** (the body isn't validated or compared). Log `bank_stub.transfer.replayed`; metric outcome `replayed`.
5. Validate the body with `TransferRequest.model_validate_json(raw)`. Failure → 422 `validation_error` with `details=[{field, issue}]` (field names and error types only; values never echoed).
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
request body via `openapi_extra`, plus the 201 and 200 `TransferResponse` responses.

| Status | Code | When |
|---|---|---|
| 201 | — | New transfer (settled or rejected: a rejection is still 2xx) |
| 200 | — | Repeated key: the stored row |
| 400 | `idempotency_key_required` | `Idempotency-Key` header missing (C34) |
| 401 | `invalid_api_key` | Missing or wrong `X-Api-Key` |
| 413 | `payload_too_large` | Body over 256 KB (pycommon `MAX_BODY_BYTES`) |
| 422 | `validation_error` | Malformed `Idempotency-Key` (C34), or an invalid body on a new key |
| 500 | `internal_error` | Unhandled |
| 503 | `db_timeout`, `db_unavailable` | Statement or pool timeout; DB down |

The worker classifies only a 2xx with `status: rejected` and a `reason` as a definite rejection;
every other status here is "no answer" to it (ADR 0003 e).

### 3.2 `GET /v1/transfers/{key}` (PAY-412)

Headers: `X-Api-Key` (required). Path `key` must match `IDEMPOTENCY_KEY_PATTERN` (the `:` in the worker's key is a valid path character).
1. `require_api_key` → 401 `invalid_api_key`.
2. Invalid key format (`IDEMPOTENCY_KEY_RE.fullmatch`, so `…%0A` is refused too) → 422 `validation_error` with `details=[{"field": "path.key", "issue": "pattern"}]` (R3, C14; the field path follows C56).
3. Sleep `BANK_STUB_LATENCY_MS`.
4. `get_by_key(key)` → 200 `TransferResponse`, or 404 `transfer_not_found`.

This is the provider-adapter lookup: the worker uses it after the key safe window (PAY-410), and tests and operators use it to reconcile.

### 3.3 API key check (`api/auth.py`)

```python
class InvalidApiKey(AppError):                  # pycommon.errors.AppError
    status = 401
    code = "invalid_api_key"

async def require_api_key(x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
                          settings: Settings = Depends(get_settings)) -> None:
    if x_api_key is None or not compare_digest(          # from hmac import compare_digest
            x_api_key.encode(), settings.bank_stub_api_key.get_secret_value().encode()):
        raise InvalidApiKey("API key missing or invalid.")
```

- It applies to both `/v1/transfers` routes, not to `/livez` or `/readyz`.
- A failure logs `bank_stub.auth.rejected` with `reason` (`missing` or `mismatch`), never the key.
- pycommon's redactor masks `x-api-key` and `api_key` in request logs and spans.

### 3.4 Outcome function (`domain/outcome.py`)

```python
Outcome = tuple[Literal["settled", "rejected"], Literal["account_closed", "insufficient_funds_at_bank"] | None]

def decide_outcome(key: str, account: str, failure_rate: float) -> Outcome:
    if account.endswith("0000"):
        return ("rejected", "account_closed")
    bucket = int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest(), "big") % 10_000
    if bucket < Decimal(repr(failure_rate)) * 10_000:   # exact PAY-412 comparison (C63)
        return ("rejected", "insufficient_funds_at_bank")
    return ("settled", None)

def bank_reference_for(key: str) -> str:
    return "BSR-" + hashlib.sha256(b"ref:" + key.encode("utf-8")).hexdigest()[:16].upper()
```

- The account rule is checked first. `account_last4 == "0000"` ⇔ `account.endswith("0000")`.
- The failure check is integer arithmetic on the key only. The same key always gives the same outcome, whatever the replica, restart or body, so a retry with a new attempt (new key) can get a different outcome.
- The threshold is the exact PAY-412 comparison `bucket < BANK_STUB_FAILURE_RATE × 10000`, in decimal arithmetic on the configured value (no rounding, no binary-float error; C63): 0.05 gives 500, so buckets 0–499 are rejected; 0.00025 gives 2.5, so buckets 0–2 are. Rate 0 never rejects; rate 1 always does.
- The reference format is `BSR-` plus 16 uppercase hex characters (20 in all, ≤ 64 per C26), derived from the key (deterministic; stored anyway).
- `transfer_id` is a fresh UUIDv7 (`pycommon.db.new_id()`) per inserted row.

### 3.5 Health and startup checks (PAY-605)

- `/livez`: 200 while the process is up.
- `/readyz` (pycommon `health_router`): 200 when `db` (`SELECT 1`) and `migrations` (`bank_stub.alembic_version` at head) pass; otherwise 503. bank-stub has no Redis, so there is no Redis check (R6, PAY-605).
- **Settings validation** (all environments): `BANK_STUB_FAILURE_RATE` in [0, 1] and `BANK_STUB_LATENCY_MS` in [0, 10000] are field constraints. A failure is pycommon's `settings.invalid` (field names only) and exit 2.
- **Startup checks** (C29): pycommon `run_startup_checks(settings, extra=[check_not_prod])`, run before anything connects. In uat and prod each failure logs `startup.check_failed` {`setting` (the env var **name**, never the value), `reason`} and the process exits with code **2**; in dev failures are logged at warning.
  - `check_not_prod`: `ENV=prod` fails with `setting="ENV"`, `reason="not_deployable"` (bank-stub is never deployed in prod, ADR 0001).
  - pycommon's common checks: every `SecretStr` present, not a dev default and ≥ 16 characters (`DB_PASSWORD`, `BANK_STUB_API_KEY`); `DEBUG` and `DOCS_ENABLED` false.

### 3.6 Latency

Every `/v1/transfers` request (POST and GET, after the API-key and key checks) awaits
`asyncio.sleep(BANK_STUB_LATENCY_MS / 1000)` before touching the database. The default is 20 ms
(plan §2.2). Larger delays and faults (timeouts, resets, outages) come from Toxiproxy in the `test`
profile, not from bank-stub settings.

## 4. Configuration

`Settings` subclasses pycommon `BaseServiceSettings` (`service_name="bank-stub"`). The pycommon names
are the contract (C1): there is no `DATABASE_URL` or `MIGRATOR_DATABASE_URL`. Service-only keys use
the `BANK_STUB_` prefix. Redis keys aren't read (`use_redis=False`). Compose takes the common keys
from `x-py-env`.

| Key / env var | Type | Default (dev / uat / prod) | Requirement |
|---|---|---|---|
| `ENV` | `dev\|uat` | dev / uat / not deployed (`prod` refused) | PAY-605, ADR 0001 |
| `APP_VERSION` | str | git SHA / image tag | PAY-NFR-10 |
| `LOG_LEVEL` | `DEBUG\|INFO\|WARNING\|ERROR` (case-insensitive, C24) | `INFO` | PAY-NFR-07 |
| `DEBUG` / `DOCS_ENABLED` | bool | false, true / false (refused if true in uat) | PAY-605 |
| `COMPANY_ID` | UUID | `0193a000-0000-7000-8000-000000000001` (C28) / per env | required by `BaseServiceSettings`; unused by bank-stub |
| `DB_HOST` / `DB_PORT` / `DB_NAME` | str / int / str | `postgres` / 5432 / `payrun` (dev); managed endpoint (uat) | PAY-606, C1 |
| `DB_USER` | str | `bank_stub_app` (service); `bank_stub_migrator` (migrate container) | PAY-606, C2 |
| `DB_PASSWORD` | secret | `${BANK_STUB_DB_PASSWORD}` (service), `${BANK_STUB_DB_MIGRATOR_PASSWORD}` (migrate) / External Secrets | PAY-606 |
| `DB_SCHEMA` | str | `bank_stub` | PAY-606 |
| `DB_STATEMENT_TIMEOUT_MS` / `DB_POOL_TIMEOUT_S` | int / float | 2000 / 2 | plan §2.2 |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | int | 20 / 5 | PAY-412 (concurrent POSTs) |
| `BANK_STUB_API_KEY` | secret | `.env` (`dev-env.sh`) / External Secrets | PAY-412 |
| `BANK_STUB_FAILURE_RATE` | float [0, 1] | 0.05 / 0.05 | PAY-412 |
| `BANK_STUB_LATENCY_MS` | int [0, 10000] | 20 / 20 | PAY-412 |
| `PORT` / `PYCOMMON_APP` | int / str | 8000 / `bank_stub.main:app` (Dockerfile) | — |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | URL | `http://otel-collector:4317` | PAY-NFR-09 |
| `OTEL_TRACES_SAMPLER` | str | `parentbased_always_on` (no `OTEL_TRACES_SAMPLER_ARG`; C23) | PAY-NFR-09 |
| `OTEL_METRIC_EXPORT_INTERVAL` | int ms | 15000 | PAY-NFR-10 |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | str | `http` (Dockerfile) | observability |

Port 8000 in the container; host 4113 in the Compose test override (plan §13).

## 5. Logging, metrics and spans

Logs are JSON through `pycommon.logging`, with `trace_id`. Logs and spans never contain the API
key, routing, the full account or the amount (PAY-NFR-07). The idempotency key
(`<payout_id>:<attempt>`) and `transfer_id` are IDs, not personal data, and are logged.

### 5.1 Log events

| Event | Level | Fields | When | Requirement |
|---|---|---|---|---|
| `bank_stub.transfer.created` | info | `transfer_id`, `idempotency_key`, `status`, `reason` | New row inserted (201) | PAY-412 |
| `bank_stub.transfer.replayed` | info | `transfer_id`, `idempotency_key`, `status` | Repeated key (200), including a lost concurrent insert | PAY-412 |
| `bank_stub.transfer.not_found` | info | `idempotency_key` | GET 404 | PAY-412 |
| `bank_stub.auth.rejected` | warning | `reason` (missing, mismatch), `path` | 401 | PAY-412 |
| `settings.invalid` | error | `fields` (names and error types only) | Settings validation failed; exit 2 (pycommon) | PAY-605 |
| `startup.check_failed` | error (uat, prod; then exit 2) / warning (dev) | `setting` (name only), `reason` | A startup check failed (pycommon, C29) | PAY-605 |
| `http.request` / `http.error` | info (warning on 5xx) / info, warning | pycommon standard (route, status, duration_ms) / code, status | Every request / an `AppError` (400, 401, 404, 422) | plan §11 |
| `health.check_failed` | warning | `check`, `error_type` | `/readyz` failure (pycommon) | PAY-605 |
| `db.timeout` / `db.unavailable` | warning | `operation` | DB timeout / connection failure (pycommon) | PAY-605 |

### 5.2 Metrics

| Metric | Type | Labels (values) | When | Requirement |
|---|---|---|---|---|
| `bank_stub_transfers_total` | counter | `outcome` (settled, rejected, replayed) | Once per POST that returns 200/201 | PAY-412 (R6) |
| `http_server_request_duration_seconds` | histogram | `http_route`, `http_request_method`, `http_response_status_code` (auto) | Every request | plan §11 |
| `db_client_connections_usage` | gauge (auto) | `state` | SQLAlchemy pool | PAY-NFR-10 |

The counter is created as `bank_stub_transfers` (the Collector appends `_total`; platform §3.3).
Rejection reasons aren't a label here: the worker's `payouts_bank_transfers_total{outcome, reason}`
already carries them.

### 5.3 Spans

bank-stub exports every span (`parentbased_always_on`, C23); in uat the Collector's
`tail_sampling` applies (R27).

- **Server span** (pycommon FastAPI instrumentation, kind SERVER): `POST /v1/transfers` or `GET /v1/transfers/{key}`. Its parent is the incoming `traceparent` from the worker's per-try client span, so bank-stub joins the J4 trace (PAY-NFR-09). Attributes are the stable HTTP semantic conventions only (`http.request.method`, `http.route`, `http.response.status_code`, `url.scheme`, `server.address`); `url.query` is dropped and no request or response header is captured, so `X-Api-Key` never reaches a span. `/livez` and `/readyz` aren't traced.
- **`bank_stub.transfer`** (manual, kind INTERNAL, child of the server span): covers steps 4–8 of §3.1 or step 4 of §3.2. Attributes: `payrun.bank_operation` (`create`, `get`), `payrun.idempotency_key`, `payrun.transfer_id` (when known), `payrun.bank_result` (`settled`, `rejected`, `replayed`, `not_found`). Status ERROR only for a 5xx.
- **DB spans** (SQLAlchemy instrumentation, `enable_commenter=False`): children of `bank_stub.transfer`, one per statement (`SELECT` for the replay lookup, `INSERT … ON CONFLICT`, the fallback `SELECT`). They carry the statement text with bind placeholders, never parameter values.
- The latency sleep (§3.6) has no span of its own; it shows as time in the server span before `bank_stub.transfer` starts.
- No span attribute or event holds the API key, routing, account, `account_last4` or amount. Validation errors put field names only on the span (as an exception event without the message).

## 6. Tests

Unit tests have no I/O. Route tests use `tests/helpers.py::stubbed()`: the real app (routes,
middleware, error handlers) with a **stub lifespan** and a `Runtime` over an in-memory repository
and a recording sleep, so no pycommon lifecycle runs (no logging reconfiguration, engine,
startup checks or loop-lag ticker; C63). Assertions that a value is never echoed exclude random
or time fields (`request_id`, `ts`): the C61 flake was `"-5"` matching a request ID and `"1.5"`
matching a timestamp. Integration tests use pycommon's fixtures (`pg_bootstrapped`,
`alembic_upgrade`): a session-scoped Testcontainers `postgres:16-alpine`. The fixture runs
`deploy/compose/init/*.sql`, migrates as `bank_stub_migrator`, connects as `bank_stub_app` through
the C1 keys, and truncates `bank_stub.transfers` after each test. The app is driven through
`httpx.AsyncClient(transport=ASGITransport(app))`.

| Test file | Covers | Requirement |
|---|---|---|
| `tests/unit/test_outcome.py` | Account ending `0000` → `account_closed` (even at rate 0); the same key always gives the same outcome; rate 0 never rejects, rate 1 always does; boundary: a key with bucket 499 is rejected at 0.05 and a key with bucket 500 isn't; no rounding (bucket 2 rejected at 0.00025, bucket 1 at 0.00015; C63); over 10,000 random keys at 0.05 the rejected share is 5% ± 1%; reference format `^BSR-[0-9A-F]{16}$` (≤ 64) | PAY-412 |
| `tests/unit/test_schemas.py` | `TransferRequest` bounds and patterns, `extra="forbid"`; `repr` masks routing and account; key pattern accepts `<uuid>:<n>`; `TransferResponse` serialises exactly `{transfer_id, status, reason, bank_reference}` (R6) | PAY-412, NFR-07 |
| `tests/unit/test_routes.py` (API key) | Missing → 401; wrong → 401; correct → passes; uses `compare_digest` (patched as the module-local name `bank_stub.api.auth.compare_digest`, never the stdlib function) | PAY-412 |
| `tests/unit/test_latency.py` | The handler awaits `sleep(BANK_STUB_LATENCY_MS / 1000)` (patched sleep) on POST and GET | PAY-412 |
| `tests/unit/test_settings.py` | Settings load from the C1 keys (`DB_HOST` … `DB_SCHEMA`); `DATABASE_URL` is ignored; `LOG_LEVEL=info` is accepted as `INFO` (C24); rate and latency out of bounds → `settings.invalid` and exit 2 | PAY-605 |
| `tests/unit/test_startup_checks.py` | `ENV=prod` → `startup.check_failed` (`setting=ENV`) and exit 2; with `ENV=uat`, a missing or dev-default `BANK_STUB_API_KEY` or `DB_PASSWORD`, and debug or docs on, each exit 2; the log names the setting, never the value; with `ENV=dev` failures only warn (C29) | PAY-605 |
| `tests/integration/test_migrations.py` | Upgrade to head, downgrade one, upgrade again; constraints (unique key, outcome shape, `USD` only) | PAY-606 |
| `tests/integration/test_grants.py` | `bank_stub_app` gets `permission denied` on `payroll.*`, `payouts.*`, `identity.*` | PAY-606 |
| `tests/integration/test_transfers_api.py` | POST new → 201 settled with a reference; account `…0000` → 201 rejected `account_closed`; repeated key → 200, the same `transfer_id`, even with a different (or invalid) body; every 2xx body has exactly the four R6 fields; GET → 200 / 404 `transfer_not_found`; missing API key → 401 on both routes; missing `Idempotency-Key` → **400 `idempotency_key_required`**; malformed `Idempotency-Key` → 422 `validation_error` with `details=[{field: "Idempotency-Key", issue}]`; malformed GET key → 422 `validation_error`; invalid body on a new key → 422 `validation_error` with field names and no echoed values (C14, C34) | PAY-412 |
| `tests/integration/test_concurrent_same_key.py` | 50 concurrent POSTs with one key through **two app instances** (separate engines and pools, like two replicas) → exactly one row; every response has the same `transfer_id`, `status` and `bank_reference`; exactly one 201; `bank_stub_transfers_total` grows by one `settled`/`rejected` and the rest `replayed` | PAY-412, NFR-02 |
| `tests/integration/test_restart_persistence.py` | POST a key, shut down the app and dispose its engine, build a new app → GET returns the same row, and a repeated POST returns 200 with the same `transfer_id` | PAY-412, NFR-02 |
| `tests/integration/test_readyz.py` | 503 when the DB is down or migrations aren't at head; no Redis needed | PAY-605 |
| `tests/integration/test_spans.py` | In-memory exporter: a POST with an incoming `traceparent` yields a server span whose parent and trace ID are the caller's; a `bank_stub.transfer` child with the attributes of §5.3 for created, replayed and GET-not-found; DB spans under it; no span carries the API key, routing, account, last 4 or amount | PAY-NFR-09, NFR-07 |
| `tests/integration/test_no_secrets_in_logs.py` | Captured logs and spans for POST/GET/401/422 contain none of: the API key, routing, full account, amount | PAY-NFR-07 |
| `tests/integration/test_metrics.py` | In-memory reader: `bank_stub_transfers_total{outcome}` values after created, rejected and replayed calls | PAY-412 |
| `tests/contract/test_openapi.py` | `openapi.json` matches the app (drift); Schemathesis against the app finds no 5xx and no schema violations | PAY-NFR-12 |

The Compose-level restart scenario (bank-stub restarted mid-run, ≤ 1 settled transfer per payout)
is owned by the worker's resilience suite: `tests/resilience/test_bank_restart.py`.

## 7. Traceability

| Requirement | Design section | Tests |
|---|---|---|
| PAY-412: POST with `Idempotency-Key` and `X-Api-Key`, ON CONFLICT, 201 new / 200 replay, lookup before body validation | §3.1, §3.3 | `test_transfers_api.py`, `test_api_key.py` |
| PAY-412: response `{transfer_id, status, reason, bank_reference}` | §2.3 | `test_schemas.py`, `test_transfers_api.py` |
| PAY-412: missing `Idempotency-Key` → 400, malformed → 422 (C34) | §3.1 step 2 | `test_transfers_api.py` |
| PAY-412: outcome rules | §3.4 | `test_outcome.py` |
| PAY-412: latency | §3.6 | `test_latency.py` |
| PAY-412: GET 200/404 | §3.2 | `test_transfers_api.py` |
| PAY-412: missing API key → 401 | §3.3 | `test_api_key.py`, `test_transfers_api.py` |
| PAY-412: concurrent POSTs → one row | §3.1 step 7 | `test_concurrent_same_key.py` |
| PAY-412: ledger survives restart | §2.1, §3.1 | `test_restart_persistence.py`, `tests/resilience/test_bank_restart.py` |
| PAY-412: `bank_stub_transfers_total{outcome}` (R6) | §5.2 | `test_metrics.py`, `test_concurrent_same_key.py` |
| PAY-605 | §3.5, §4 | `test_settings.py`, `test_startup_checks.py`, `test_readyz.py` |
| PAY-606 | §2.1, §2.2, §4 | `test_migrations.py`, `test_grants.py` |
| PAY-NFR-02 (bank side) | §3.1, §3.4 | `test_concurrent_same_key.py`, `test_restart_persistence.py` |
| PAY-NFR-07 | §2.3, §5 | `test_schemas.py`, `test_spans.py`, `test_no_secrets_in_logs.py` |
| PAY-NFR-09 | §5.3 | `test_spans.py`, e2e `trace.spec.ts` |

## Change log

- 2026-11-10: incorporated README R-table and C-table decisions (R3, R6, R13, R27; C1, C2, C14, C23, C24, C28, C29, C34, C44).
- 2026-11-10 (Step 5, C63): exact PAY-412 failure threshold (no `round`); keys checked with `fullmatch`; GET key error field `path.key` (C56); module-local `compare_digest`; unit route tests on a stub lifespan; the C61 unit flake traced to value-echo assertions matching `request_id`/`ts` and fixed.

Resolved after the change log entry: C46 (`PAYOUT_PAYROLL_*` keys), C47 (`COMPANY_ID` for every service, `PAYOUT_CONSUMER_GC_IDLE_S`, startup reason `not_deployable`). Nothing is open.
