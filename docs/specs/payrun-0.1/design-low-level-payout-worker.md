# Low-level design: payout-worker

Status: draft 2026-11-10, aligned with the README Spec resolutions (R) and Contract decisions (C) tables · Sources: [spec README](README.md) (R- and C-tables), [architecture plan](../../plan/architecture-plan.md) v1.2 (§2.1, §2.2, §4, §5 J4, §6, §9, §11, §12.1), [ADR 0003](../../adr/0003-payout-flow.md), [ADR 0001](../../adr/0001-service-map.md), [ADR 0007](../../adr/0007-data-stores.md), [ADR 0011](../../adr/0011-egress.md), [requirements](requirements.md) (PAY-402–412, PAY-600, PAY-605–608, PAY-NFR-02/03/07/09/10/11), [high-level design](design-high-level.md), [payroll low-level design](design-low-level-payroll.md) (§4.6, §4.7, §5: the internal endpoints and the event), [platform low-level design](design-low-level-platform.md) (pycommon, env, ACLs).

The worker consumes `payout.requested` v1 from `stream:payout.requested` (group `payout-worker`).
On every delivery it fetches the instruction from payroll, calls the bank with
`Idempotency-Key = <payout_id>:<attempt>`, classifies the answer and posts the result back to
payroll. It never writes payroll's tables, never stores account numbers, and never reports
`failed`: an uncertain answer is `indeterminate` (ADR 0003 d). Its only HTTP surface is
`/livez` and `/readyz`.

## 1. Modules

All paths are under `services/payout_worker/`. The package is `payout_worker` (Compose and Kubernetes name: `payout-worker`).

| Module (path) | New / changed | Responsibility |
|---|---|---|
| `src/payout_worker/main.py` | New | Builds the health-only app with `pycommon.app.create_app(settings, use_jwt=False, migrations_script_location="migrations", extra_health_checks=…, startup_checks=[…] (§3.7), background=[consumer, gauge poller, maintenance], on_startup=…, shutdown_timeout_s=PAYOUT_SHUTDOWN_DRAIN_S)`: only `/livez` and `/readyz`, no routers. pycommon's lifespan runs the startup checks before any connection, sets up logging and OTel, opens the DB engine and the command Redis client; `on_startup` opens the blocking-read Redis client (§3.3.1), ensures the consumer group and pre-initialises the counters (§5.2). On shutdown pycommon sets the shared `stop` event, which runs `Consumer.stop()` (§3.3.7), then closes the clients. Entry point: `python -m pycommon.serve` with `PYCOMMON_APP=payout_worker.main:app`, `PORT=8000`, `timeout_graceful_shutdown=70`. |
| `src/payout_worker/consumer.py` | New | `Consumer` (the worker's own; not pycommon's, C21): group creation and NOGROUP recovery, the pending-first reader, the `>` reader, the XAUTOCLAIM loop, delivery counts from XPENDING, the concurrency semaphore, the budget decision after a failed delivery (`decide_after_failure`), DLQ writes, XACK, the lag/DLQ gauge poller, graceful stop. |
| `src/payout_worker/handler.py` | New | `PayoutHandler.handle(msg, delivery) -> HandleResult`: the per-message algorithm of PAY-403 (§3.1). Pure orchestration over the ports `PayrollPort`, `BankPort`, `DisbursementRepo`, `Clock`; no Redis calls (it returns an action the consumer executes). |
| `src/payout_worker/classify.py` | New | `classify_transfer_response(...)` and `classify_lookup_response(...)`: PAY-404 rules (§3.5.3). Pure functions. |
| `src/payout_worker/models.py` | New | Pydantic models: `Envelope`, `PayoutRequestedData`, `Instruction`, `ResultRequest`, `ResultResponse`, `TransferRequest`, `TransferResponse`, and the answer types `Settled`, `Rejected`, `NoAnswer`, `NotFound` (§2.3). `parse_envelope()` validates against `contracts/events/payout.requested.v1.json` first, then Pydantic. |
| `src/payout_worker/errors.py` | New | Worker exception set (§3.2): `InvalidMessage`, `AttemptNotCurrent`, `PayoutNotFound`, `PayrollUnavailable`, `PayrollRejected`, `BankNoAnswer`, `WorkerInternalError`. |
| `src/payout_worker/clients/payroll.py` | New | `PayrollClient`: `get_instruction()` and `post_result()` on pycommon's `ResilientClient.request_raw` (provider `payroll`), with a JWT minted per HTTP try through `auth_headers` (§3.4). |
| `src/payout_worker/clients/bank.py` | New | `BankClient`: `create_transfer()` and `get_transfer()` on `ResilientClient.request_raw` (provider `bank`), the ADR 0003 (e) override: non-2xx responses are returned to `classify.py`, never mapped to `ProviderRejected` (§3.5). |
| `src/payout_worker/jwt_signer.py` | New | `WorkerJwtSigner.mint(company_id) -> str`: EdDSA token, `iss=payout-worker`, `sub=service:payout-worker`, `aud=payroll`, `cid`, 60 s (§3.6). |
| `src/payout_worker/db/models.py` | New | SQLAlchemy Core tables `payouts.disbursements` and `payouts.processed_events` (§2.1). The worker's DDL owns both; pycommon defines no processed-events table (C22). |
| `src/payout_worker/db/repo.py` | New | `DisbursementRepo`: `is_processed`, `upsert_submitting`, `record_answer`, `finish`, `purge_processed_events` (§3.1.1). |
| `src/payout_worker/settings.py` | New | `Settings(BaseServiceSettings)` (pycommon; no env prefix; names exactly as §4, C1) and the worker's extra startup checks (§3.7). |
| `src/payout_worker/metrics.py` | New | Instruments of §5.2, created once from the global meter; `preinit()` adds 0 to every `queue_messages_total` series at start (C21); observable-gauge callbacks read the cached values the poller writes. |
| `src/payout_worker/maintenance.py` | New | The tasks run by pycommon's `MaintenanceRunner` (lock `payout-worker:maintenance`, every `MAINTENANCE_INTERVAL_S`): purge `processed_events` older than `RETENTION_PROCESSED_EVENTS_DAYS` (PAY-608), and remove idle consumers with no pending entries from the group (§3.8). |
| `migrations/` (`env.py`, `versions/0001_payouts_initial.py`) | New | Alembic via `pycommon.db.run_alembic_env(metadata, schema="payouts", runtime_role="payouts_app")`; version table `payouts.alembic_version` (§2.2). |
| `tests/{unit,integration,contract}/` | New | §6. Resilience tests live in the repo-level `tests/resilience/`. |
| `Dockerfile` | New | Non-root, read-only root filesystem; `ENV OTEL_SEMCONV_STABILITY_OPT_IN=http`. |

## 2. Data models

### 2.1 Schema `payouts`

Owned by the migrator role `payouts_migrator` (password env `PAYOUTS_DB_MIGRATOR_PASSWORD`); the
runtime role `payouts_app` (password env `PAYOUTS_DB_PASSWORD`) has `USAGE` on the schema and DML on
its tables through the default privileges set by `deploy/compose/init/30-payouts.sql` (PAY-606, C2).
The service connects with the C1 keys (`DB_USER`, `DB_PASSWORD`, …; §4), never a URL. Neither
table holds account numbers, names or amounts (plan §6). Both tables are defined here, not in
pycommon (C22). The extra `disbursements` columns (`company_id`, `reason`, `last_event_id`,
`deliveries`) are accepted by R13.

```sql
CREATE TABLE payouts.disbursements (
  id              uuid        PRIMARY KEY,              -- UUIDv7, set by the app
  payout_id       uuid        NOT NULL,
  attempt         smallint    NOT NULL CHECK (attempt >= 1),
  company_id      uuid        NOT NULL,
  status          text        NOT NULL CHECK (status IN ('submitting', 'done')),
  outcome         text        NULL CHECK (outcome IN
                    ('settled', 'rejected', 'indeterminate', 'poison', 'attempt_not_current')),
  reason          text        NULL CHECK (char_length(reason) <= 100),
  bank_reference  text        NULL CHECK (char_length(bank_reference) <= 64),
  last_event_id   uuid        NOT NULL,
  deliveries      integer     NOT NULL DEFAULT 1 CHECK (deliveries >= 1),
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT disbursements_payout_attempt_uq UNIQUE (payout_id, attempt),
  CONSTRAINT disbursements_done_has_outcome CHECK (status <> 'done' OR outcome IS NOT NULL)
);

CREATE TABLE payouts.processed_events (
  event_id      uuid        PRIMARY KEY,
  stream        text        NOT NULL,                  -- 'stream:payout.requested'
  payout_id     uuid        NOT NULL,
  attempt       smallint    NOT NULL CHECK (attempt >= 1),
  company_id    uuid        NOT NULL,
  outcome       text        NOT NULL CHECK (outcome IN
                  ('settled', 'rejected', 'indeterminate', 'poison', 'attempt_not_current')),
  processed_at  timestamptz NOT NULL DEFAULT now(),
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX processed_events_processed_at_idx ON payouts.processed_events (processed_at);
```

Column notes:
- `disbursements` is a record of tries, not a gate (plan §2.1: a `done` row never blocks a new event; the upsert resets it to `submitting`). `deliveries` counts upserts across all events for that attempt. `reason` holds the bank's rejection reason or the poison reason. `outcome` and `bank_reference` are written as soon as the bank answers (`record_answer`), before the result post, so an operator can see a settled transfer whose result post is still pending.
- `bank_reference` is at most 64 characters, as everywhere in the system (C26).
- `processed_events` is written only for messages that were fully handled and ACKed through the normal path (§3.1 step 8). Dead-lettered messages are **not** recorded, so an operator's DLQ replay of the same `event_id` (runbook `PayoutDLQ`) is not skipped.
- `updated_at` is set by the repository on every UPDATE (no trigger).
- Retention: `processed_events` 30 days (`RETENTION_PROCESSED_EVENTS_DAYS`, purged by `processed_at`); `disbursements` kept (plan §6).

### 2.2 Migration

`services/payout_worker/migrations/versions/0001_payouts_initial.py`, revision `0001_payouts_initial`, `down_revision = None`.
- `env.py` calls `pycommon.db.run_alembic_env(metadata, schema="payouts", runtime_role="payouts_app")`: `version_table_schema="payouts"`, `include_schemas=True`, filtered to `payouts`. It connects with the same C1 keys as the service, set to the migrator's credentials in the migrate container (`DB_USER=payouts_migrator`, `DB_PASSWORD=${PAYOUTS_DB_MIGRATOR_PASSWORD}`, `DB_SCHEMA=payouts`). After `upgrade` the helper revokes `INSERT, UPDATE, DELETE` on `payouts.alembic_version` from `payouts_app`. The schema itself and the roles come from the bootstrap SQL, not from this migration.
- `upgrade()`: creates both tables, the constraints and the index exactly as §2.1.
- `downgrade()`: drops `processed_events`, then `disbursements`.
- Runs as the Compose one-shot `migrate-payouts` and the Kubernetes Job `migrate-payouts-<version>`, before the worker starts.
- Later changes are expand-then-contract (plan §6).

### 2.3 Wire models (`models.py`)

All models use `ConfigDict(extra="forbid", frozen=True)` for what the worker sends, and `extra="ignore"` for what it receives (so additive provider/payroll changes don't break it). Money is `int` cents; no floats.

**Consumed event** (PAY-402; JSON Schema `contracts/events/payout.requested.v1.json` is checked first, then):

```python
class PayoutRequestedData(BaseModel):          # extra="forbid", matches the JSON Schema
    payout_id: UUID
    pay_run_id: UUID
    company_id: UUID
    attempt: int = Field(ge=1)
    amount_cents: int = Field(gt=0, le=1_000_000_000)
    currency: Literal["USD"]
    republish_seq: int = Field(ge=0)

class Envelope(BaseModel):
    event_id: UUID                              # UUIDv7
    type: Literal["payout.requested"]
    version: Literal[1]
    occurred_at: AwareDatetime
    producer: Literal["payroll"]
    traceparent: str = Field(pattern=r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")   # required (C22)
    data: PayoutRequestedData
```

**Stream entry** (R7, PAY-402): three fields written by payroll's relay through pycommon
`StreamProducer.publish` (C22): `event_id` (string), `type` (`payout.requested`) and `envelope`
(the envelope JSON). The `envelope` field is authoritative; the `event_id` field is used only as a
fallback ID when the envelope can't be parsed (§3.1 step 0). A missing `envelope` field, invalid
JSON, a schema violation (including a missing or malformed `traceparent`) or a Pydantic error
raises `InvalidMessage`, which keeps whatever of `payout_id`, `attempt`, `company_id` and
`event_id` could be read from the raw JSON (for the poison report, §3.1 step 0).

**Instruction** (PAY-411; `GET /v1/internal/payouts/{id}/instruction?attempt=n` → 200):

```python
class Instruction(BaseModel):                  # extra="ignore"
    payout_id: UUID
    attempt: int = Field(ge=1)
    amount_cents: int = Field(gt=0)
    currency: Literal["USD"]
    routing: SecretStr                          # 9 digits; never logged, never in spans
    account: SecretStr                          # 4–17 digits
    requested_at: AwareDatetime
```

`routing` and `account` are `SecretStr`, unwrapped only inside `BankClient.create_transfer` when
the request body is built. A 200 body that fails validation is treated as a payroll error
(`PayrollRejected`, code `malformed_instruction`).

**Result** (PAY-405; `POST /v1/internal/payouts/{id}/result`):

```python
Outcome = Literal["settled", "rejected", "indeterminate", "poison"]
AttemptStatus = Literal["requested", "indeterminate", "needs_operator", "settled", "rejected"]
FINAL_STATUSES: Final = frozenset({"settled", "rejected"})

class ResultRequest(BaseModel):                # extra="forbid"
    attempt: int = Field(ge=1)
    outcome: Outcome
    reason: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,100}$")
    bank_reference: str | None = Field(default=None, min_length=1, max_length=64)   # C26

    # settled → bank_reference required, reason None; rejected → reason required;
    # poison → reason in {"schema_invalid", "amount_mismatch", "key_window_expired"};
    # indeterminate → reason and bank_reference None.

class ResultResponse(BaseModel):               # extra="ignore"; 200 body (R7, PAY-405)
    payout_id: UUID
    attempt: int
    outcome: AttemptStatus                      # the stored attempt status: any of the five (C26)
```

Every 200 is success, whatever `outcome` holds (C26). The worker treats `settled` and `rejected`
as final and every other status (`requested`, `indeterminate`, `needs_operator`) as "recorded";
in both cases it finishes and ACKs. When the stored status is final and differs from the posted
outcome (for example `indeterminate` posted after `settled`), it logs
`payout.result_already_final`. payroll's `ResultOut` may carry more fields (`applied`,
`payout_status`, `run_status`); `extra="ignore"` drops them.

**Bank** (PAY-412; `POST /v1/transfers`, `GET /v1/transfers/{key}`):

```python
class TransferRequest(BaseModel):              # extra="forbid"
    amount_cents: int = Field(gt=0)            # the instruction's amount, never the event's
    currency: Literal["USD"]
    routing: str = Field(pattern=r"^\d{9}$")
    account: str = Field(pattern=r"^\d{4,17}$")
    # __repr__ masks routing and account

class TransferResponse(BaseModel):             # extra="ignore"; R6: 201 new, 200 replay
    transfer_id: UUID
    status: Literal["settled", "rejected"]
    reason: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,100}$")
    bank_reference: str | None = Field(default=None, min_length=1, max_length=64)   # C26
```

A body that fails this model (for example a `bank_reference` over 64 characters, or a `reason`
the result endpoint can't accept) is `NoAnswer("malformed_body")` (§3.5.3), never a definite answer.

**Answer types** (output of `classify.py`):

```python
@dataclass(frozen=True) class Settled:  transfer_id: UUID; bank_reference: str
@dataclass(frozen=True) class Rejected: transfer_id: UUID; reason: str
@dataclass(frozen=True) class NoAnswer: reason: NoAnswerReason   # see §3.5.3
@dataclass(frozen=True) class NotFound: pass                      # GET by key only
```

## 3. Interfaces

### 3.1 Handler algorithm (PAY-403)

`PayoutHandler.handle(msg: StreamMessage, delivery: int) -> HandleResult`, where `delivery` is
the message's delivery count (§3.3.3) and `HandleResult` is one of `Ack(outcome)`,
`DeadLetter(kind, code)` (DLQ then ACK) or `Retry(kind, code)` (leave pending). The whole call
runs under `asyncio.timeout(HANDLER_DEADLINE_S)`, where
`HANDLER_DEADLINE_S = BANK_TOTAL_DEADLINE_S + 2 × PAYOUT_PAYROLL_CALL_DEADLINE_S + 10` (35 s by default). The handler records the instruction fetch time and never **starts** a bank POST later than `HANDLER_DEADLINE_S` after it; payroll's `mark_rejected` wait depends on this bound (C48);
the startup invariant `PAYOUT_CLAIM_MIN_IDLE_MS` > that value (plan §2.2) guarantees a message is
never reclaimed while a live handler can still be working on it.

`key = f"{payout_id}:{attempt}"` (ADR 0003 a). `ctx` is the consume span (§5.3).

0. **Parse.** `env = parse_envelope(msg)`.
   - `InvalidMessage` → poison, dead-lettered **at once** whatever the delivery count (R5,
     PAY-408). If `payout_id`, `attempt` and `company_id` are all readable, first report `poison`
     with reason `schema_invalid` (as in step 7, key `<event_id>:poison`; `event_id` comes from
     the envelope, else from the entry's `event_id` field, else the stream entry ID is used in
     its place). A 200 (any stored outcome) or a 409 from payroll both count as delivered. Then
     return `DeadLetter("invalid", "schema_invalid")`. If the report fails with
     `PayrollUnavailable` or `PayrollRejected`, the payroll rules of §3.2 apply (leave pending,
     then DLQ at the budget). Nothing is written to the database.
   - The consume span is started here as a child of `env.traceparent` (PAY-NFR-09). For an
     `InvalidMessage` whose `traceparent` can't be read from the raw JSON, it is a root span and
     the log `queue.message.trace_missing` is written.
1. **Processed?** `if await repo.is_processed(env.event_id)` → `Ack("duplicate")`. No DB write, no HTTP call.
2. **Upsert the try.** `await repo.upsert_submitting(env)` (§3.1.1). Committed on its own.
3. **Instruction, on every delivery.** `ins = await payroll.get_instruction(payout_id, attempt, company_id)`.
   - `AttemptNotCurrent` (409 `attempt_not_current`) → `repo.finish(env, "attempt_not_current")`, log `payout.attempt_not_current`, `Ack("attempt_not_current")`. **No bank call.**
   - `PayoutNotFound` (404 `payout_not_found`: unknown payout or another company's) → poison, `DeadLetter("invalid", "payout_not_found")` at once (R5). No poison report: payroll doesn't know the payout for this `cid`, so the report would get the same 404.
   - `PayrollUnavailable` / `PayrollRejected` propagate (§3.2).
4. **Amount check.** If `ins.amount_cents != env.data.amount_cents`, or `ins.currency`, `ins.payout_id` or `ins.attempt` differ from the event → report `poison` with reason `amount_mismatch` (step 7 rules; a 200 with any stored outcome and a 409 are both accepted), `repo.record_answer(..., outcome="poison", reason="amount_mismatch")`, log `payout.poison_reported`, return `DeadLetter("invalid", "amount_mismatch")` at once (R5, PAY-409). The amounts themselves are never logged.
5. **Key safe window (PAY-410).** If `clock.now() - ins.requested_at > timedelta(seconds=BANK_KEY_SAFE_WINDOW_S)`:
   - `resp = await bank.get_transfer(key)`; `answer = classify_lookup_response(resp)`.
   - `NotFound` → report `poison` with reason `key_window_expired`, `repo.finish(env, "poison", reason="key_window_expired")`, log `payout.key_window_expired`, `Ack("poison")`. **Never POST.** The message is not dead-lettered (it isn't invalid; the attempt becomes `needs_operator`).
   - `Settled` / `Rejected` → continue at step 7 with that answer.
   - `NoAnswer` → step 6's no-answer rule.
6. **Transfer.** Otherwise `resp = await bank.create_transfer(key, TransferRequest(amount_cents=ins.amount_cents, currency=ins.currency, routing=ins.routing.get_secret_value(), account=ins.account.get_secret_value()))`; `answer = classify_transfer_response(resp)`.
   - Log `bank.transfer.submitted` for `Settled`, `bank.transfer.rejected` for `Rejected`, `bank.transfer.no_answer` for `NoAnswer`; increment `payouts_bank_transfers_total`.
   - **No answer:** if `delivery < PAYOUT_MAX_DELIVERIES` → return `Retry("bank", answer.reason)` (the message stays pending and is reclaimed after `PAYOUT_CLAIM_MIN_IDLE_MS`). If `delivery >= PAYOUT_MAX_DELIVERIES` → the outcome becomes `indeterminate` (PAY-404, PAY-408); log `payout.indeterminate`. Never `rejected`, never `failed`.
7. **Classify and post the result (PAY-404, PAY-405).**
   - `repo.record_answer(payout_id, attempt, outcome, reason, bank_reference)` (status stays `submitting`).
   - `res = await payroll.post_result(payout_id, company_id, ResultRequest(attempt, outcome, reason, bank_reference), idempotency_key=f"{event_id}:{outcome}")`, mapping `Settled` → `settled` + `bank_reference`, `Rejected` → `rejected` + `reason`, budget-exhausted `NoAnswer` → `indeterminate`.
   - 200 → continue, whatever `res.outcome` is (C26). If `res.outcome` is `settled` or `rejected` and differs from the posted outcome, log `payout.result_already_final`. Any other stored status (`requested`, `indeterminate`, `needs_operator`) is "recorded": no extra log. For example, posting `indeterminate` or `poison` for a `needs_operator` attempt returns 200 with `needs_operator` and no change (R7).
   - `AttemptNotCurrent` (409) → treated as final (plan §5 J4 "late result for an old attempt"); continue with the posted outcome recorded.
   - `PayrollUnavailable` / `PayrollRejected` propagate (§3.2). The bank answer is not lost: the redelivery re-POSTs with the same key and the bank returns the original transfer.
8. **Finish, in this order:**
   1. One DB transaction: `UPDATE disbursements SET status='done', outcome, reason, bank_reference` and `INSERT INTO processed_events … ON CONFLICT (event_id) DO NOTHING` (`repo.finish`).
   2. Return `Ack(outcome)`; the consumer then runs `XACK stream:payout.requested payout-worker <id>`.

   A crash between 8.1 and the XACK leaves the message pending; its redelivery stops at step 1 and is ACKed. A crash anywhere before 8.1 repeats the whole algorithm, which is safe because steps 3 and 6 are idempotent (instruction gate plus one bank key per attempt) and step 7's key is `<event_id>:<outcome>`.

After `handle()` returns, the consumer executes the action (§3.3.4): `Ack` → XACK; `DeadLetter` → XADD to `stream:payout.requested.dlq`, then XACK; `Retry` → `decide_after_failure()`.

#### 3.1.1 Repository (`db/repo.py`)

```python
class DisbursementRepo:
    async def is_processed(self, event_id: UUID) -> bool
    async def upsert_submitting(self, env: Envelope) -> None
        # INSERT INTO payouts.disbursements (id, payout_id, attempt, company_id, status, last_event_id)
        # VALUES (:uuid7, :payout_id, :attempt, :company_id, 'submitting', :event_id)
        # ON CONFLICT (payout_id, attempt) DO UPDATE SET status='submitting', outcome=NULL, reason=NULL,
        #   last_event_id=EXCLUDED.last_event_id, deliveries=payouts.disbursements.deliveries + 1, updated_at=now()
        # (bank_reference is kept)
    async def record_answer(self, payout_id: UUID, attempt: int, outcome: str,
                            reason: str | None, bank_reference: str | None) -> None
    async def finish(self, env: Envelope, outcome: str, reason: str | None = None,
                     bank_reference: str | None = None) -> None   # one transaction, step 8.1
    async def purge_processed_events(self, older_than: datetime) -> int
```

Every statement runs with `statement_timeout = DB_STATEMENT_TIMEOUT_MS`; a pool wait over
`DB_POOL_TIMEOUT_S` or a statement timeout raises `WorkerInternalError("db_timeout")`.

### 3.2 Failures and what the worker does

`decide_after_failure(kind, delivery)` in `consumer.py`:

| Exception / result | Raised by | `kind` | `delivery < PAYOUT_MAX_DELIVERIES` | `delivery >= PAYOUT_MAX_DELIVERIES` |
|---|---|---|---|---|
| `InvalidMessage` (poison) | step 0 | invalid | DLQ + ACK at once (after the poison report, when the IDs are readable) | same |
| `AttemptNotCurrent` (instruction) | step 3 | — | finish, ACK (not a failure) | same |
| `AttemptNotCurrent` (result) | step 7 | — | finish, ACK | same |
| `PayoutNotFound` (poison) | step 3 | invalid | DLQ + ACK at once | same |
| amount / currency / id mismatch (poison) | step 4 | invalid | poison report, DLQ + ACK at once | same |
| `BankNoAnswer` (classified `NoAnswer`, including an open breaker) | steps 5–6 | bank | leave pending (`Retry`) | post `indeterminate`, finish, ACK |
| `PayrollUnavailable` (timeout, connect error, 429/502/503/504 after retries, other 5xx, breaker open) | steps 0, 3, 4, 5, 7 | payroll | leave pending | DLQ + ACK |
| `PayrollRejected` (400 `idempotency_key_required`, 401 `invalid_token`, 403 `forbidden_role`, 404 on the result, 422, other unexpected 4xx, malformed 200 body) | steps 0, 3, 4, 5, 7 | payroll | leave pending | DLQ + ACK |
| `WorkerInternalError` (DB error or timeout, `HANDLER_DEADLINE_S` exceeded) | any | internal | leave pending | DLQ + ACK |
| Unexpected `Exception` | any | internal | log with traceback, leave pending | DLQ + ACK |
| `asyncio.CancelledError` (shutdown) | any | — | leave pending, re-raise | same |
| Redis error on XACK | consumer | — | message stays pending; the redelivery stops at step 1 and ACKs | same |

"Leave pending" means no XACK: the entry stays in the group's PEL and is reclaimed by
XAUTOCLAIM once idle for `PAYOUT_CLAIM_MIN_IDLE_MS`. So, as R5 decides: poison (schema-invalid,
amount mismatch, `payout_not_found` on the instruction) is dead-lettered at once; payroll
unreachable or rejecting, and worker DB failures, stay pending and are dead-lettered only after
`PAYOUT_MAX_DELIVERIES`. The bank row (no answer at the budget) is the only case that ACKs without
the DLQ; payroll then re-publishes the attempt with the same bank key (PAY-407). A posting failure
of that `indeterminate` result is a payroll failure on the last delivery, so the message goes to
the DLQ and the attempt stays `requested` for payroll's re-publish.

**DLQ entry** (C21; schema [`contracts/events/payout.requested.dlq.v1.json`](../../../contracts/events/payout.requested.dlq.v1.json)):
`XADD stream:payout.requested.dlq MAXLEN ~ 10000 * envelope … source_id … error_kind … error_code … error … delivery_count … consumer … failed_at …`.
Exactly these eight fields, all strings:

| Field | Value |
|---|---|
| `envelope` | the original entry's `envelope` field, byte for byte (IDs and amount only; no bank details); `""` when the entry had no `envelope` field |
| `source_id` | the original stream entry ID |
| `error_kind` | `invalid` \| `payroll` \| `internal` |
| `error_code` | `schema_invalid`, `amount_mismatch`, `payout_not_found` (kind `invalid`); the payroll code, e.g. `payroll_unavailable`, `breaker_open`, `invalid_token`, `forbidden_role`, `idempotency_key_required`, `validation_error`, `malformed_response` (kind `payroll`); `db_timeout`, `db_unavailable`, `handler_deadline`, `unexpected` (kind `internal`) |
| `error` | short description: the exception class name plus a fixed message for the code, passed through pycommon `redact`, ≤ 500 characters; never response bodies, bank details, amounts or tokens |
| `delivery_count` | the delivery number, decimal |
| `consumer` | this consumer's name (`PAYOUT_CONSUMER_NAME`) |
| `failed_at` | RFC 3339 UTC with `Z` |

`MAXLEN ~ 10000` bounds the DLQ; operators drain it per the `PayoutDLQ` runbook, and
`queue_dlq_messages` shows its length. Order: XADD to the DLQ, then XACK. A crash between them
yields a duplicate DLQ entry with the same `source_id`, never a lost message.
`queue_messages_total{outcome="dlq"}` is incremented after the XADD succeeds (alert `PayoutDLQ`).
If the DLQ XADD itself fails, the message is left pending (no XACK) and retried by XAUTOCLAIM.

### 3.3 Consumer loop and delivery accounting (PAY-403, PAY-408)

#### 3.3.1 Redis clients

Both clients are built by pycommon `create_redis(settings, …)` from the C1 keys `REDIS_HOST`,
`REDIS_PORT`, `REDIS_USERNAME` (`payout-worker`) and `REDIS_PASSWORD`; there is no `REDIS_URL`.

- **Command client:** pycommon's default client (`socket_timeout = REDIS_SOCKET_TIMEOUT_MS`, 500 ms), created by `create_app`. Used for XACK, XAUTOCLAIM, XPENDING, XINFO, XGROUP CREATE, XGROUP DELCONSUMER, XADD to the DLQ, and PING (`/readyz`).
- **Blocking-read client:** `create_redis(settings, socket_timeout_ms=PAYOUT_XREAD_BLOCK_MS + 1000, client_name="payout-worker-reader")` (6 s by default), the same C1 credentials, used only for `XREADGROUP … BLOCK`, so a normal block isn't cut off by the 500 ms command timeout.
- Commands used, all granted to the `payout-worker` ACL user (R12, C27; platform §2.2): XREADGROUP, XACK, XAUTOCLAIM, XPENDING, XINFO GROUPS / STREAM / CONSUMERS, XGROUP CREATE (with MKSTREAM, which needs write access to `stream:payout.requested`), XGROUP DELCONSUMER (§3.8), XADD and XINFO on `stream:payout.requested.dlq`, PING. The user can't XADD to `stream:payout.requested` or touch any other key.

#### 3.3.2 Start-up and reading

1. `XGROUP CREATE stream:payout.requested payout-worker 0 MKSTREAM`; a `BUSYGROUP` error is ignored. `0`, not `$`, so events published before the group existed are delivered.
2. **Pending first:** `last = "0"`; loop `XREADGROUP GROUP payout-worker <me> COUNT <free> STREAMS stream:payout.requested <last>` until it returns no entries, setting `last` to the last returned ID each time. An entry returned with a nil body (trimmed from the stream) is XACKed and logged as `queue.message.trimmed`; payroll's re-publish covers it.
3. **Then new entries:** loop `XREADGROUP GROUP payout-worker <me> COUNT <free> BLOCK PAYOUT_XREAD_BLOCK_MS STREAMS stream:payout.requested >`.

`<me>` is `PAYOUT_CONSUMER_NAME` (default: the hostname, i.e. the pod name in Kubernetes or the container hostname in Compose). `<free>` is the number of free semaphore slots; the reader waits for at least one free slot before each read, so it never holds more than `PAYOUT_CONCURRENCY` messages. A Redis connection error in any loop logs `queue.read.error` and backs off with full jitter (0.5 s doubling to 30 s), then resumes.

#### 3.3.3 Delivery counts

- An entry read with `>` is delivery 1 (no extra round trip).
- For an entry from the pending-first read or from XAUTOCLAIM, the worker calls `XPENDING stream:payout.requested payout-worker <id> <id> 1` after the read or claim and uses the returned `times-delivered` as `delivery`. XAUTOCLAIM increments the counter; whether a history read (step 2) increments it is pinned by `test_delivery_counts.py`. If it does not, the count is conservative by one (one extra try), which is safe.
- If XPENDING returns nothing (the entry was ACKed by another consumer meanwhile), the entry is skipped.

#### 3.3.4 After each message

The counter's `outcome` label takes exactly `ok`, `retry` and `dlq` (C21); all three series are
pre-initialised to 0 at start (§5.2).

| `HandleResult` | Consumer action | `queue_messages_total{outcome}` |
|---|---|---|
| `Ack(outcome)` (any handler outcome, including `duplicate`, `attempt_not_current`, `poison` from the key window, and `indeterminate`) | XACK | `ok` |
| `DeadLetter(kind, code)` | XADD to DLQ, XACK, log `queue.message.failed` (error, `dlq=true`) | `dlq` |
| `Retry(kind, code)` with `delivery < PAYOUT_MAX_DELIVERIES` | nothing (stays pending); log `queue.message.failed` (warning, `dlq=false`) | `retry` |
| `Retry("payroll" \| "internal", …)` at the budget | XADD to DLQ, XACK | `dlq` |

(`Retry("bank", …)` is never returned at the budget: the handler converts it to `indeterminate` in step 6.)
`queue_processing_seconds` is observed for every handled message, whatever the result.

#### 3.3.5 XAUTOCLAIM loop

Every `PAYOUT_CLAIM_INTERVAL_S` (10 s), while there are free slots:
`XAUTOCLAIM stream:payout.requested payout-worker <me> PAYOUT_CLAIM_MIN_IDLE_MS <cursor> COUNT <free>`,
starting at cursor `0-0` and following the returned cursor until it is `0-0` or no slot is free.
- Each claimed entry gets its delivery count (§3.3.3) and is handed to the handler.
- Entry IDs XAUTOCLAIM reports as deleted (trimmed while pending) are logged as `queue.message.trimmed`; Redis has already removed them from the PEL.
- The claim recovers both this consumer's failed messages and messages of dead consumers (another replica or a previous pod name).
- A failed message is retried between `PAYOUT_CLAIM_MIN_IDLE_MS` and `PAYOUT_CLAIM_MIN_IDLE_MS + PAYOUT_CLAIM_INTERVAL_S` after its last delivery (60–70 s by default). With `PAYOUT_MAX_DELIVERIES` = 5, a message with an unreachable bank is reported `indeterminate` about 4–5 minutes after its first delivery (plan §5 J4).
- A message whose ID is in this process's in-flight set is not handed over twice (it is released by the running handler).

#### 3.3.6 NOGROUP recovery

Any `NOGROUP` error from XREADGROUP, XAUTOCLAIM or XPENDING (group or stream lost: FLUSHALL,
failover to an empty replica) triggers, once per error burst:
`XGROUP CREATE stream:payout.requested payout-worker 0 MKSTREAM` (BUSYGROUP ignored, since another
replica may have recreated it), log `queue.group.recreated` (warning, fields `stream`, `group`),
then the reader restarts at pending-first. In-flight handlers continue; their XACK against the new
group returns 0 and is harmless. Entries still in the stream are re-delivered from `0` and stop at
step 1 if already processed. Lost entries are redriven by payroll's re-publish (PAY-407).

#### 3.3.7 Concurrency and graceful shutdown

- `asyncio.Semaphore(PAYOUT_CONCURRENCY)` (16) bounds handlers across the reader and the claim loop together. One task per message; slots are released in `finally`.
- SIGTERM → uvicorn's lifespan shutdown → pycommon sets the shared `stop` event → `Consumer.stop()`:
  1. Set `stopping`; cancel the blocking reader and the claim loop, so no new message is read or claimed (log `worker.shutdown.started`, field `in_flight`).
  2. Wait for in-flight handlers up to `PAYOUT_SHUTDOWN_DRAIN_S` (60 s; a handler is bounded by `HANDLER_DEADLINE_S` = 35 s, so normally all finish).
  3. Cancel any still running; their messages stay pending and are reclaimed by another consumer.
  4. The poller and the maintenance runner stop on the same event; pycommon then closes both Redis clients, disposes the DB engine and flushes telemetry (log `worker.shutdown.completed`, fields `drained`, `cancelled`, `duration_s`, before the flush).
  The total stays within `terminationGracePeriodSeconds: 75` (plan §2.2): 60 s drain plus ≤ 10 s for flush and close. `/readyz` returns 503 from step 1 on.

#### 3.3.8 Gauge poller

Every `PAYOUT_LAG_POLL_S` (15 s): `XINFO GROUPS stream:payout.requested` → the `lag` of `payout-worker` (nil after trimming → keep the last value); `XINFO STREAM stream:payout.requested.dlq` → `length` (no such key → 0). The values are cached for the observable gauges of §5.2. Every replica exports the same values; alerts use `max()`.

### 3.4 Payroll client (`clients/payroll.py`)

```python
class PayrollClient:
    def __init__(self, settings: Settings, signer: WorkerJwtSigner,
                 transport: httpx.AsyncBaseTransport | None = None) -> None
    async def get_instruction(self, payout_id: UUID, attempt: int, company_id: UUID) -> Instruction
        # raises AttemptNotCurrent | PayoutNotFound | PayrollUnavailable | PayrollRejected
    async def post_result(self, payout_id: UUID, company_id: UUID, body: ResultRequest,
                          idempotency_key: str) -> ResultResponse
        # raises AttemptNotCurrent | PayrollUnavailable | PayrollRejected
```

- **Requests:**
  - `GET {PAYOUT_PAYROLL_BASE_URL}/v1/internal/payouts/{payout_id}/instruction?attempt={attempt}` (operation `get_instruction`).
  - `POST {PAYOUT_PAYROLL_BASE_URL}/v1/internal/payouts/{payout_id}/result` with `ResultRequest` JSON and `Idempotency-Key: <event_id>:<outcome>` (operation `post_result`).
  - Headers on both: `Authorization: Bearer <jwt>` minted per HTTP try through `ResilientClient(auth_headers=…)` (§3.6) with `cid` = the event's `company_id`; `Accept: application/json`; `traceparent`/`tracestate` injected by the httpx instrumentation; `X-Request-Id: <event_id>`. The POST's `Idempotency-Key` is passed as `idempotency_key=`, so it is the same on every try.
- **Policy** (`ProviderPolicy(name="payroll")`): connect 1 s (`PAYOUT_PAYROLL_CONNECT_TIMEOUT_S`), read 2 s (`PAYOUT_PAYROLL_READ_TIMEOUT_S`), up to 3 retries (`PAYOUT_PAYROLL_MAX_RETRIES`) with full-jitter backoff (base 0.1 s, cap 1 s, `Retry-After` honoured when it fits in the remaining deadline) on 429/502/503/504, **connect errors and read timeouts** (R10: both calls are idempotent, a GET and a POST with an `Idempotency-Key`). Total deadline `PAYOUT_PAYROLL_CALL_DEADLINE_S` (5 s, R2) around all tries. Breaker per target `payroll` (pycommon): opens after `BREAKER_FAILURES` (5) consecutive failures (timeouts, connect errors, 5xx; 429 and other 4xx don't count), half-opens after `BREAKER_RESET_S` (30 s).
- **Response mapping** (the client calls `request_raw` and maps the final response itself; codes per payroll §4.6, §4.7 and C30, C34):

  | Response | Exception / value |
  |---|---|
  | 200 with a valid body (any stored `outcome`, C26) | `Instruction` / `ResultResponse` |
  | 200 with an invalid body | `PayrollRejected("malformed_response")` |
  | 409 `attempt_not_current` | `AttemptNotCurrent` |
  | 404 `payout_not_found` (instruction) | `PayoutNotFound` |
  | 400 `idempotency_key_required`, 401 `invalid_token`, 403 `forbidden_role`, 404 (result), 409 other code, 422 (`validation_error`, `idempotency_key_reused`), other 4xx | `PayrollRejected(<code>)` (`http_<status>` when the body has no code) |
  | 429, 5xx (after retries), timeout, connect error, deadline, breaker open | `PayrollUnavailable(<code>)` |

### 3.5 Bank client (`clients/bank.py`)

```python
class BankClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None
    async def create_transfer(self, key: str, body: TransferRequest) -> BankCallResult
    async def get_transfer(self, key: str) -> BankCallResult

@dataclass(frozen=True)
class BankCallResult:
    response: httpx.Response | None      # the last response, if any
    error: ProviderError | None          # ProviderTimeout | ProviderUnavailable (incl. breaker open) | ConnectError
```

The bank client calls pycommon `ResilientClient.request_raw`, so it never raises for HTTP status
codes and never maps 4xx to `ProviderRejected`: it returns the last response or transport error,
and `classify.py` decides (ADR 0003 e).

#### 3.5.1 Requests

- `POST {BANK_BASE_URL}/v1/transfers` (operation `create_transfer`), body `TransferRequest`.
- `GET {BANK_BASE_URL}/v1/transfers/{key}` (operation `get_transfer`), the key URL-path-encoded.
- Headers: `Idempotency-Key: <payout_id>:<attempt>` (POST; the same value on every retry and every delivery), `X-Api-Key: <BANK_STUB_API_KEY>`, `Content-Type: application/json` (POST), `Accept: application/json`, `User-Agent: payrun-payout-worker/<version>`, `traceparent`/`tracestate` (instrumentation). The API key is never logged; the redactor masks the header in error logs.

#### 3.5.2 Policy

`ProviderPolicy(name="bank")`: connect 1 s (`BANK_CONNECT_TIMEOUT_S`), read 5 s
(`BANK_READ_TIMEOUT_S`), up to 3 retries (`BANK_MAX_RETRIES`) with full jitter (base 0.2 s, cap
2 s, `Retry-After` honoured within the deadline) on 429/502/503/504, connect errors and timeouts,
always with the same `Idempotency-Key`. Total deadline `BANK_TOTAL_DEADLINE_S` (15 s), retries
included; each try's read timeout is `min(BANK_READ_TIMEOUT_S, remaining)`. Other statuses (500,
4xx) are not retried within the call; they are "no answer" and are retried by redelivery. Breaker
per target `bank` (pycommon; `BREAKER_FAILURES` 5, `BREAKER_RESET_S` 30 s): failures are
timeouts, connect errors and 5xx. A 2xx (settled or rejected), a 429 and other 4xx don't count.
While the breaker is open, calls fail at once with `ProviderUnavailable("breaker_open")`.

#### 3.5.3 Classification (`classify.py`, PAY-404)

```python
NoAnswerReason = Literal["timeout", "connect_error", "breaker_open", "http_429", "http_5xx",
                         "http_401", "http_403", "http_404", "http_409", "http_422",
                         "http_4xx_other", "http_other", "malformed_body"]

def classify_transfer_response(r: BankCallResult) -> Settled | Rejected | NoAnswer
def classify_lookup_response(r: BankCallResult) -> Settled | Rejected | NotFound | NoAnswer
```

Rules, in order:
1. `r.error` set → `NoAnswer` with `timeout`, `connect_error` or `breaker_open`.
2. Status 2xx (201 for a new transfer, 200 for a replay or a lookup; R6): parse `TransferResponse` `{transfer_id, status, reason, bank_reference}`.
   - `status == "settled"` and a `bank_reference` of 1–64 characters → `Settled`.
   - `status == "rejected"` and a non-empty `reason` matching `^[a-z0-9_]{1,100}$` → `Rejected` (the **only** definite rejection).
   - anything else (invalid JSON, unknown status, `rejected` without a valid reason, `settled` without a reference, a reference over 64 characters) → `NoAnswer("malformed_body")`.
3. Lookup only: 404 → `NotFound`.
4. Every other status → `NoAnswer` with its reason (`http_429`, `http_5xx`, `http_401`, `http_403`, `http_404` for POST, `http_409`, `http_422`, `http_4xx_other`, `http_other` for 1xx/3xx).

Nothing in this module can return `rejected` for a non-2xx response; a unit test per row proves it.

### 3.6 JWT signer (`jwt_signer.py`, PAY-600)

```python
class WorkerJwtSigner:
    def __init__(self, private_key_pem: SecretStr, kid: str, ttl_s: int = 60, clock: Clock = SystemClock()) -> None
    def mint(self, company_id: UUID) -> str
```

- Header: `{"alg": "EdDSA", "typ": "JWT", "kid": PAYOUT_WORKER_JWT_KID}`.
- Claims (R9, PAY-600): `iss="payout-worker"`, `sub="service:payout-worker"`, `aud="payroll"`, `cid=str(company_id)` (from the event, never `COMPANY_ID`), `roles=[]`, `iat=now`, `exp=now+60` (`PAYOUT_WORKER_JWT_TTL_S`), `jti=uuid7()`. No `sid` and no `name`.
- payroll answers a bad token (expired, wrong kid or issuer) with 401 `invalid_token`, and a valid token of the wrong kind with 403 `forbidden_role` (C30); both are `PayrollRejected` (§3.4).
- `iss`, `sub` and `aud` are constants in code, not settings, so the worker key can't mint other issuers' tokens; payroll's verifier also binds the kid to `iss=payout-worker` (plan §8).
- A fresh token per HTTP try (Ed25519 signing costs microseconds), so a retry never reuses a token close to expiry.

### 3.7 Health and startup checks

- `/livez`: 200 while the process is up.
- `/readyz` (pycommon `health_router`): 200 only when `db` (`SELECT 1`), `redis` (PING on the command client), `migrations` (`payouts.alembic_version` at the head revision), `consumer` (`task_check`: the reader task is alive) and `stopping` (the worker isn't stopping) all pass. Otherwise 503 with the failing check names (PAY-605).
- **Settings validation** (all environments): `Settings` declares `PAYOUT_CONCURRENCY ≥ 1`, `PAYOUT_MAX_DELIVERIES ≥ 1`, `PAYOUT_WORKER_JWT_TTL_S` in [1, 60] as field constraints, and `PAYOUT_SHUTDOWN_DRAIN_S + 10 ≤ 75` as a model validator. A failure is pycommon's `settings.invalid` (field names only) and exit 2.
- **Startup checks** (C29): pycommon `run_startup_checks(settings, extra=[…])`, run by `create_app` before anything connects. In uat and prod each failure logs `startup.check_failed` {`setting` (the env var **name** only), `reason`} and the process exits with code **2**; in dev the same failures are logged at warning and start-up continues (PAY-605).
  - pycommon's common checks: every `SecretStr` present, not a dev default, ≥ 16 characters (`DB_PASSWORD`, `REDIS_PASSWORD`, `PAYOUT_WORKER_JWT_PRIVATE_KEY`, `BANK_STUB_API_KEY`); `PAYOUT_WORKER_JWT_KID` doesn't start with `dev-` or `test-`; `DEBUG` and `DOCS_ENABLED` are false.
  - `check_claim_idle`: `PAYOUT_CLAIM_MIN_IDLE_MS / 1000 > BANK_TOTAL_DEADLINE_S + 2 × PAYOUT_PAYROLL_CALL_DEADLINE_S + 10` (60 > 35 with the defaults).
  - `check_republish_after`: `PAYOUT_REPUBLISH_AFTER_S > PAYOUT_MAX_DELIVERIES × PAYOUT_CLAIM_MIN_IDLE_MS / 1000 + BANK_TOTAL_DEADLINE_S` (360 > 315 with the R1 default). The worker reads `PAYOUT_REPUBLISH_AFTER_S` only for this check; payroll runs the same check, and both read the shared `payout-timing` keys (R11).
  - `check_bank_base_url` (prod): the `BANK_BASE_URL` host isn't `bank-stub`, `localhost`, `example.*`, `*.invalid` or a placeholder (reason `bank_stub_url`).
  - The worker's own `check_key_window_explicit` (prod): `BANK_KEY_SAFE_WINDOW_S` is set in the environment, not taken from the code default (`"bank_key_safe_window_s" in settings.model_fields_set`; reason `missing`).
- **Prod in 0.1.0** (R14, C32): the prod overlay points `BANK_BASE_URL` at a placeholder and sets the `payout-worker` Deployment to `replicas: 0` until a bank-provider ADR exists, so `check_bank_base_url` never crash-loops a pod. `tests/platform/test_k8s_policy.py` asserts the 0 replicas.

### 3.8 Maintenance (PAY-608)

`maintenance.py` provides two tasks for pycommon's `MaintenanceRunner(engine, sessions, lock_name="payout-worker:maintenance", tasks=[…], interval_s=MAINTENANCE_INTERVAL_S)`.
The runner ticks every 60 s ± 10% under `pg_try_advisory_lock(advisory_lock_key("payout-worker:maintenance"))`,
so one replica at a time; each task runs in its own transaction.

1. **`purge_processed_events`:** `DELETE FROM payouts.processed_events WHERE event_id IN (SELECT event_id FROM payouts.processed_events WHERE processed_at < now() - make_interval(days => :RETENTION_PROCESSED_EVENTS_DAYS) LIMIT 1000)`, repeated until fewer than 1000 rows go; returns the row count.
2. **`prune_consumers`** (uses the R12/C27 `XGROUP DELCONSUMER` grant; the session is unused): `XINFO CONSUMERS stream:payout.requested payout-worker`, then `XGROUP DELCONSUMER stream:payout.requested payout-worker <name>` for every consumer with `pending == 0` and `idle > PAYOUT_CONSUMER_GC_IDLE_S × 1000`, never this consumer's own name. Consumers with pending entries are never deleted (DELCONSUMER would drop their PEL entries); XAUTOCLAIM moves those entries to a live consumer first. Returns the number deleted. This stops old pod names piling up in the group.

Logs and metrics are pycommon's (C44): `maintenance.task.completed` {`task`, `affected`, `duration_ms`} at info and `maintenance.task.failed` {`task`, `error_type`} at error; `maintenance_runs_total{task, outcome}` and `maintenance_duration_seconds{task}`. A failing task doesn't stop the other.

## 4. Configuration

`Settings` subclasses pycommon `BaseServiceSettings` (`service_name="payout-worker"`, no env
prefix). The pycommon names are the contract (C1): there is no `DATABASE_URL`, `MIGRATOR_DATABASE_URL`
or `REDIS_URL`. Precedence: process environment > `.env` (dev and CI) > code defaults. Every key is
in `.env.example`. Compose takes the common keys from `x-py-env` and the timing keys from
`x-payout-timing`; Kubernetes from the shared ConfigMaps (`payout-timing`) and External Secrets.

| Key / env var | Type | Default (dev / uat / prod) | Requirement |
|---|---|---|---|
| `ENV` | `dev\|uat\|prod` | dev / uat / prod | PAY-605 |
| `APP_VERSION` | str | git SHA / image tag / image tag | PAY-NFR-10 |
| `LOG_LEVEL` | `DEBUG\|INFO\|WARNING\|ERROR` (case-insensitive, C24) | `INFO` (all) | PAY-NFR-07 |
| `DEBUG` / `DOCS_ENABLED` | bool | false / false (refused if true in uat, prod) | PAY-605 |
| `COMPANY_ID` | UUID | `0193a000-0000-7000-8000-000000000001` (C28) / per env / per env | required by `BaseServiceSettings`; the worker takes `cid` from each event, not from this key |
| `DB_HOST` / `DB_PORT` / `DB_NAME` | str / int / str | `postgres` / 5432 / `payrun` (dev); managed endpoint (uat, prod) | PAY-606, C1 |
| `DB_USER` | str | `payouts_app` (service); `payouts_migrator` (migrate container) | PAY-606, C2 |
| `DB_PASSWORD` | secret | `${PAYOUTS_DB_PASSWORD}` (service), `${PAYOUTS_DB_MIGRATOR_PASSWORD}` (migrate) / External Secrets / External Secrets | PAY-606 |
| `DB_SCHEMA` | str | `payouts` (all) | PAY-606 |
| `DB_STATEMENT_TIMEOUT_MS` / `DB_POOL_TIMEOUT_S` | int / float | 2000 / 2 (all) | plan §2.2 |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | int | 20 / 5 (all): 16 handlers + maintenance + readyz + spare; 2 replicas × 25 stays under the role's 60-connection limit | PAY-403 |
| `REDIS_HOST` / `REDIS_PORT` | str / int | `redis` / 6379 (dev); managed endpoint (uat, prod) | plan §6, C1 |
| `REDIS_USERNAME` | str | `payout-worker` (all) | PAY-607 |
| `REDIS_PASSWORD` | secret | `${REDIS_PAYOUT_WORKER_PASSWORD}` / External Secrets / External Secrets | PAY-607 |
| `REDIS_SOCKET_TIMEOUT_MS` | int | 500 (all) | plan §2.2 |
| `PAYOUT_XREAD_BLOCK_MS` | int | 5000 (all) | plan §2.2 |
| `PAYOUT_STREAM` / `PAYOUT_DLQ_STREAM` / `PAYOUT_GROUP` | str | `stream:payout.requested` / `stream:payout.requested.dlq` / `payout-worker` (all) | PAY-403, 408 |
| `PAYOUT_CONSUMER_NAME` | str | hostname (all) | PAY-403 |
| `PAYOUT_CONCURRENCY` | int ≥ 1 | 16 (all) | PAY-403 |
| `PAYOUT_MAX_DELIVERIES` | int ≥ 1 | 5 (all; shared `payout-timing`) | PAY-404, 408 |
| `PAYOUT_CLAIM_MIN_IDLE_MS` | int | 60000 (all; shared) | PAY-408 |
| `PAYOUT_CLAIM_INTERVAL_S` | float | 10 (all) | PAY-408 |
| `PAYOUT_SHUTDOWN_DRAIN_S` | float | 60 (all) | PAY-408 |
| `PAYOUT_LAG_POLL_S` | float | 15 (all) | PAY-NFR-10 |
| `PAYOUT_CONSUMER_GC_IDLE_S` | int | 3600 (all) | PAY-608 (§3.8) |
| `PAYOUT_REPUBLISH_AFTER_S` | int | 360 (all; shared; R1); read only by the startup check | PAY-605 |
| `PAYOUT_PAYROLL_BASE_URL` | URL | `http://payroll:8000` (all) | PAY-411 |
| `PAYOUT_PAYROLL_CONNECT_TIMEOUT_S` / `PAYOUT_PAYROLL_READ_TIMEOUT_S` | float | 1 / 2 (all) | plan §4 |
| `PAYOUT_PAYROLL_MAX_RETRIES` | int | 3 (all) | plan §4, R10 |
| `PAYOUT_PAYROLL_CALL_DEADLINE_S` | float | 5 (all; shared; R2) | plan §2.2 |
| `PAYOUT_WORKER_JWT_PRIVATE_KEY` | secret (PKCS8 PEM) | `.env` (`dev-env.sh`) / External Secrets / External Secrets | PAY-600 |
| `PAYOUT_WORKER_JWT_KID` | str | `dev-<date>` / required, not `dev-`/`test-` / same | PAY-600, 605 |
| `PAYOUT_WORKER_JWT_TTL_S` | int | 60 (all; max 60) | PAY-600 |
| `BANK_BASE_URL` | URL | `http://bank-stub:8000` / `http://bank-stub:8000` / provider URL (placeholder refused) | PAY-605, plan §2.2 |
| `BANK_STUB_API_KEY` | secret | `.env` / External Secrets / External Secrets | PAY-412 |
| `BANK_CONNECT_TIMEOUT_S` / `BANK_READ_TIMEOUT_S` | float | 1 / 5 (all) | plan §9 |
| `BANK_MAX_RETRIES` | int | 3 (all) | plan §9 |
| `BANK_TOTAL_DEADLINE_S` | float | 15 (all; shared) | PAY-404 |
| `BANK_KEY_SAFE_WINDOW_S` | int | 86400 / 86400 / provider's key retention (must be set) | PAY-410 |
| `BREAKER_FAILURES` / `BREAKER_RESET_S` | int / float | 5 / 30 (all) | plan §2.2 |
| `MAINTENANCE_INTERVAL_S` | int | 60 (all) | PAY-608 |
| `RETENTION_PROCESSED_EVENTS_DAYS` | int | 30 (all) | PAY-608 |
| `PORT` / `PYCOMMON_APP` | int / str | 8000 / `payout_worker.main:app` (Dockerfile) | — |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | URL | `http://otel-collector:4317` (all) | PAY-NFR-09 |
| `OTEL_TRACES_SAMPLER` | str | `parentbased_always_on` (all; no `OTEL_TRACES_SAMPLER_ARG`; prod sampling is the Collector's `tail_sampling`, R27, C23) | PAY-NFR-09 |
| `OTEL_METRIC_EXPORT_INTERVAL` | int ms | 15000 (all) | PAY-NFR-10 |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | str | `http` (Dockerfile) | observability |

The integration fixtures (Testcontainers, in-process; never `compose.test.yaml`, which keeps production timing per C53) lower `PAYOUT_CLAIM_MIN_IDLE_MS` (e.g. 2000), `PAYOUT_CLAIM_INTERVAL_S` (0.2) and the payroll/bank deadlines so budget tests run in seconds; `ENV=dev` there, so the uat/prod checks only warn.

## 5. Logging, metrics and spans

Logs are JSON through `pycommon.logging` with `trace_id`, `span_id` and these fields as named
keys. Every log carries `event_id`, `payout_id`, `attempt` and `stream_id` where known. Logs and
spans never contain amounts, routing or account numbers, API keys, JWTs or the `Authorization`
header (PAY-NFR-07); the instruction is `SecretStr`, and the redactor masks `account`, `routing`,
`api_key`, `authorization`, `token`.

### 5.1 Log events

| Event | Level | Fields | When | Requirement |
|---|---|---|---|---|
| `queue.message.processed` | info | `outcome` (settled, rejected, indeterminate, poison, attempt_not_current, duplicate), `delivery`, `duration_ms` | After XACK on the normal path | PAY-403 |
| `queue.message.failed` | warning (retry) / error (dlq) | `error_kind`, `error_code`, `delivery`, `dlq` (bool), `source_id` | A `Retry` or a `DeadLetter` (after the DLQ XADD) | PAY-408, 409 |
| `queue.message.trimmed` | warning | `stream_id` | Pending entry with no body, or XAUTOCLAIM deleted IDs | PAY-407 |
| `queue.message.trace_missing` | warning | `stream_id` | Envelope `traceparent` unparseable | PAY-NFR-09 |
| `queue.group.recreated` | warning | `stream`, `group` | NOGROUP recovery | PAY-408 |
| `queue.read.error` | warning | `error_code`, `backoff_s` | Redis error in a loop | PAY-408 |
| `bank.transfer.submitted` | info | `transfer_id`, `operation` (create/get), `bank_reference` | Bank answered settled | PAY-404 |
| `bank.transfer.rejected` | info | `transfer_id`, `reason`, `operation` | Bank answered a definite rejection | PAY-404 |
| `bank.transfer.no_answer` | warning | `reason` (NoAnswerReason), `http_status`, `operation`, `delivery` | No answer | PAY-404 |
| `payout.attempt_not_current` | info | `source` (instruction/result) | 409 from payroll | PAY-403, 411 |
| `payout.indeterminate` | warning | `last_reason`, `delivery` | Budget reached with no bank answer | PAY-404, 408 |
| `payout.poison_reported` | error | `reason` | Poison posted to payroll | PAY-409 |
| `payout.key_window_expired` | error | `requested_at` | Key-window GET returned 404 | PAY-410 |
| `payout.result_already_final` | info | `posted`, `stored` | Result 200 whose stored status is `settled`/`rejected` and differs from the posted outcome (C26) | PAY-405 |
| `external.request.retry` / `external.request.failed` | warning | `provider`, `operation`, `attempt`, `reason`, `delay_ms` | Resilient client (pycommon) | plan §9 |
| `external.breaker.opened` / `.half_open` / `.closed` | warning / info / info | `target` | Breaker transitions (pycommon) | plan §9 |
| `settings.invalid` | error | `fields` (names and error types only) | Settings validation failed; exit 2 (pycommon) | PAY-605 |
| `startup.check_failed` | error (uat, prod; then exit 2) / warning (dev) | `setting` (name only), `reason` | A startup check failed (pycommon, C29) | PAY-605 |
| `service.started` / `service.stopped` | info | `version`, `env` | Lifespan (pycommon) | — |
| `health.check_failed` | warning | `check`, `error_type` | `/readyz` failure (pycommon) | PAY-605 |
| `worker.shutdown.started` / `.completed` | info | `in_flight` / `drained`, `cancelled`, `duration_s` | SIGTERM | PAY-408 |
| `maintenance.task.completed` / `maintenance.task.failed` | info / error | `task` (`purge_processed_events`, `prune_consumers`), `affected`, `duration_ms` / `task`, `error_type` | Each task per tick (pycommon, C44) | PAY-608 |

### 5.2 Metrics

| Metric | Type | Labels (values) | When | Requirement |
|---|---|---|---|---|
| `queue_messages_total` | counter | `stream`=`stream:payout.requested`, `group`=`payout-worker`, `outcome` (exactly `ok`, `retry`, `dlq`; C21) | Per handled message (§3.3.4); **pre-initialised** with `add(0)` for each of the three outcomes in `on_startup`, before the first read, so the first DLQ after a restart is a visible 0 → 1 increase | PAY-403, 408, PAY-NFR-11; alert `PayoutDLQ` |
| `queue_processing_seconds` | histogram (0.01–60 s) | `stream`, `group` | Per handled message | PAY-NFR-10 |
| `queue_consumer_lag` | gauge | `stream`, `group` | Poller (XINFO GROUPS `lag`) | `ConsumerLagGrowing` |
| `queue_dlq_messages` | gauge | `stream`=`stream:payout.requested.dlq` | Poller (XINFO STREAM `length`) | PAY-408 |
| `payouts_bank_transfers_total` | counter | `outcome` (settled, rejected, no_answer, not_found), `reason` (bank reason for rejected — `account_closed`, `insufficient_funds_at_bank`, else `other`; NoAnswerReason for no_answer; `none` otherwise) | After each classified bank call | PAY-404 |
| `external_requests_total` | counter | `provider` (bank, payroll; R35), `operation` (create_transfer, get_transfer, get_instruction, post_result), `outcome` (pycommon: ok, client_error, rate_limited, server_error, timeout, connect_error, breaker_open) | Per HTTP try (pycommon) | plan §11; `BankErrorRateHigh` |
| `external_request_duration_seconds` | histogram | `provider`, `operation` | Per HTTP try (pycommon) | plan §11 |
| `circuit_breaker_state` | gauge (0 closed, 1 half-open, 2 open) | `target` (bank, payroll) | On transition (pycommon) | `BankBreakerOpen` |
| `maintenance_runs_total` / `maintenance_duration_seconds` | counter / histogram | `task`, `outcome` / `task` | Per task per tick (pycommon) | PAY-608 |

A bank 2xx `rejected` is `external_requests_total{outcome="ok"}`, so `BankErrorRateHigh` excludes
business rejections. Amounts never appear as labels. Counters are created with the name minus
`_total` (the Collector appends it; platform §3.3).

### 5.3 Spans

- **`payout.requested process`** (kind CONSUMER), started with `context = TraceContextTextMapPropagator().extract({"traceparent": env.traceparent})`, so it is a **child** of the producer's context and the J4 journey stays one trace (PAY-NFR-09). It also carries a span link to the same context. Attributes: `messaging.system=redis`, `messaging.destination.name=stream:payout.requested`, `messaging.consumer.group.name=payout-worker`, `messaging.message.id=<stream id>`, `payrun.event_id`, `payrun.payout_id`, `payrun.attempt`, `payrun.delivery`, `payrun.outcome`. Status ERROR for `DeadLetter`, and for `Retry` with the error recorded.
- Children: `payout.instruction`, `payout.bank_call` (attribute `payrun.bank_operation`, `payrun.bank_answer`), `payout.report_result` (`payrun.outcome`, `payrun.stored_outcome`), `payout.dead_letter` (`payrun.error_code`), and `payout.finish`; plus the auto-instrumented httpx, SQLAlchemy and Redis spans, and the resilient client's span per try (`provider`, `operation`, `attempt`, `http.response.status_code`). Outgoing HTTP carries `traceparent`, so payroll's and bank-stub's server spans join the same trace.
- No span attribute holds an amount, routing, account, API key or token.
- Sampling: the worker exports every span (`parentbased_always_on`, C23); the Collector's `tail_sampling` decides in prod (R27).

## 6. Tests

Unit tests run without I/O (`pytest-socket`, respx, `time-machine`, fake ports). Integration tests
use pycommon's fixtures (`pg_bootstrapped`, `alembic_upgrade`, `redis_acl`): session-scoped
Testcontainers `postgres:16-alpine` and `redis:7-alpine`; the fixture runs
`deploy/compose/init/*.sql`, migrates `payouts` (and `bank_stub`, for the live bank-stub app) as
the migrator roles, connects as `payouts_app` through the C1 keys, configures the Redis ACL users
from `deploy/compose/redis/`, and truncates `payouts.*`/`bank_stub.*` and flushes Redis after each
test. Events use the dev `COMPANY_ID` `0193a000-0000-7000-8000-000000000001` (C28). HTTP: payroll is a `FakePayroll` ASGI
app implementing PAY-405/PAY-411 rules in memory (attempt state, idempotency by key, `cid`
check); the bank is either the **live bank-stub app** (`bank_stub.main.create_app`) through
`httpx.ASGITransport` on the same Postgres, or respx for fault shapes.

| Test file | Covers | Requirement |
|---|---|---|
| `tests/unit/test_classify.py` | One case per row: 201 settled; 200 settled (replay); 200 rejected with reason; rejected without reason; rejected with a reason outside `^[a-z0-9_]{1,100}$`; settled without reference; settled with a 65-character reference (→ `malformed_body`); malformed JSON; unknown status; 1xx/3xx; 401; 403; 404 (POST → no answer, GET → NotFound); 409; 422; 429; 500; 502; 503; 504; timeout; connect error; breaker open. Asserts no non-2xx ever yields `Rejected` | PAY-404 |
| `tests/unit/test_handler.py` | Every branch of §3.1 with fakes: processed skip; attempt_not_current (no bank call); payout_not_found → DLQ at once, no poison report; schema-invalid with readable IDs → poison report then DLQ at once, unreadable IDs → DLQ only, both on delivery 1; amount and currency mismatch → poison + DLQ at once; key window found settled / rejected / 404 → poison `key_window_expired` with no POST; POST settled/rejected; no answer below and at the budget (→ `indeterminate`, never `rejected`); result 409 → ACK; result 200 with each stored status (`requested`, `indeterminate`, `needs_operator`, `settled`, `rejected`) → finish and ACK, `payout.result_already_final` only for a differing final status; result key `<event_id>:<outcome>`; finish order (DB tx before ACK action) | PAY-403, 404, 405, 408, 409, 410 |
| `tests/unit/test_failure_policy.py` | `decide_after_failure` table of §3.2 for every kind × below/at budget; poison kinds dead-letter at delivery 1 | PAY-408 |
| `tests/unit/test_dlq.py` | The DLQ XADD for every `error_code` of §3.2: exactly the eight C21 fields, all strings; `MAXLEN ~ 10000` (approximate) passed; `envelope` byte-for-byte (and `""` when missing); `error` ≤ 500 characters with no amount, routing, account, token or body; XADD before XACK; a failed XADD leaves the entry pending | PAY-408, NFR-07 |
| `tests/unit/test_payroll_client.py` | respx: URL, query, headers (Bearer, Idempotency-Key, X-Request-Id); a fresh JWT per try; retries on 429/502/503/504, connect errors and read timeouts only (R10); 5 s `PAYOUT_PAYROLL_CALL_DEADLINE_S` with a fake clock; breaker opens after 5 failures (429 not counted) and half-opens after 30 s; mapping table of §3.4, including 400 `idempotency_key_required`, 401 `invalid_token`, 403 `forbidden_role` → `PayrollRejected` and a 200 `ResultResponse` with `outcome="requested"` parsing | PAY-405, 411 |
| `tests/unit/test_bank_client.py` | respx: headers (`Idempotency-Key`, `X-Api-Key`); the same key on every retry; retries on 429/502/503/504, connect errors and timeouts, not on 500/4xx; 15 s total deadline; breaker counting (2xx rejected, 429 and 4xx don't count); 4xx returned, not raised | PAY-404, 412 |
| `tests/unit/test_jwt_signer.py` | Header `alg`/`kid`; claims `iss`, `sub`, `aud`, `cid` from the event, `roles == []`, `exp − iat = 60`, unique `jti`, no `sid`, no `name` (R9); verifies with pycommon's verifier and a keyset binding the kid to `payout-worker`; rejected when the kid is bound to `web` | PAY-600 |
| `tests/unit/test_models.py` | Envelope and data validation (extra fields, attempt ≥ 1, amount bounds, currency, required `traceparent`); stream entry parsing from the three R7 fields; `ResultRequest.bank_reference` ≤ 64; `SecretStr` and `TransferRequest.__repr__` never reveal numbers | PAY-402, NFR-07 |
| `tests/unit/test_settings.py` | Settings load from the C1 keys (`DB_HOST` … `DB_SCHEMA`, `REDIS_HOST` … `REDIS_PASSWORD`); `DATABASE_URL`/`REDIS_URL` are ignored; `LOG_LEVEL=info` is accepted as `INFO` (C24); the field constraints of §3.7 fail with `settings.invalid` and exit 2, naming the field only | PAY-605 |
| `tests/unit/test_startup_checks.py` | With `ENV=uat`/`prod`, each refusal (missing or dev-default secret, `dev-` kid, debug or docs on, both timing invariants, prod stub/placeholder `BANK_BASE_URL`, prod `BANK_KEY_SAFE_WINDOW_S` not set) logs `startup.check_failed` with the setting's name, never the value, and exits 2; the defaults (claim idle 60 s, re-publish 360 s) pass; with `ENV=dev` the same failures only warn | PAY-605 |
| `tests/unit/test_metrics.py` | In-memory reader: every metric of §5.2 with exact names and label sets; right after start-up, before any message, `queue_messages_total` has the three series `ok`, `retry`, `dlq` at 0 (C21) | PAY-NFR-10, NFR-11 |
| `tests/integration/test_migrations.py` | Upgrade to head, downgrade one, upgrade; constraints of §2.1 (unique attempt, done ⇒ outcome) | PAY-606 |
| `tests/integration/test_grants.py` | `payouts_app` gets `permission denied` on `payroll.*`, `identity.*`, `bank_stub.*` | PAY-606 |
| `tests/integration/test_redis_acl.py` | The `payout-worker` ACL user can run every command of §3.3.1 (including PING, XGROUP CREATE … MKSTREAM on a missing stream, XGROUP DELCONSUMER, XINFO STREAM on the `.dlq` key; R12, C27) and can't read `web:sess:*` or XADD to `stream:payout.requested` | PAY-607 |
| `tests/integration/test_consumer.py` | End to end with FakePayroll + live bank-stub: message → settled result → done row + processed_event + XACK; duplicate delivery of the same entry and the same `event_id` → no second bank row, no second result | PAY-403 |
| `tests/integration/test_delivery_counts.py` | Delivery numbers for `>` reads, pending-first reads and XAUTOCLAIM, as returned by XPENDING on Redis 7 | PAY-408 |
| `tests/integration/test_budget.py` | Bank unreachable (respx timeout) → 5 deliveries → `indeterminate` posted, ACK, no DLQ entry; payroll unreachable → 5 deliveries → DLQ entry with the fields of §3.2, ACK, `queue_messages_total{outcome="dlq"}` +1 (from the pre-initialised 0); DB timeout → DLQ at the budget | PAY-404, 408 |
| `tests/integration/test_poison.py` | Schema-invalid entry → poison reported when IDs are readable, DLQ on the first delivery, ACK, no processed_event; amount mismatch → poison + DLQ on the first delivery; `payout_not_found` → DLQ on the first delivery; unreadable IDs → DLQ only | PAY-408, 409 |
| `tests/integration/test_key_window.py` | Attempt older than the window: existing bank row → its status reported, no POST; missing → poison `key_window_expired`, no POST (bank-stub row count unchanged) | PAY-410 |
| `tests/integration/test_crash_points.py` | Crash injected (cancel) after step 2, after the bank call, after the result, after the DB tx: redelivery yields one bank row and one applied result | PAY-403, NFR-02 |
| `tests/integration/test_two_consumers.py` | Two `Consumer`s; the first is paused after the bank call, the second XAUTOCLAIMs: one bank row, both results accepted (second gets 200 stored) | PAY-403, NFR-02 |
| `tests/integration/test_republish_race.py` | Two entries, fresh `event_id`s, same attempt: both pass the instruction check, one bank row, second result 200 with the stored outcome | PAY-405, 407 |
| `tests/integration/test_old_attempt.py` | Attempt 1 rejected and recorded, attempt 2 settled, attempt 1 redelivered → 409 `attempt_not_current`, no bank call, ACK | PAY-403, 411 |
| `tests/integration/test_nogroup.py` | `XGROUP DESTROY` and `FLUSHALL` during consumption → group recreated from `0` with MKSTREAM, `queue.group.recreated` logged, new entries consumed | PAY-408 |
| `tests/integration/test_shutdown.py` | `stop()` with 16 in-flight slow handlers: no read after stop, all finish, pending ones left unACKed when the drain times out, total under 75 s (fake clock) | PAY-408 |
| `tests/integration/test_trace_context.py` | In-memory span exporter: the consume span's parent is the envelope's span ID and trace ID; outgoing requests carry the same trace ID | PAY-NFR-09 |
| `tests/integration/test_no_secrets_in_logs.py` | Captured logs and spans of a full run contain none of: routing, account, `BANK_STUB_API_KEY`, the JWT, the amount | PAY-NFR-07 |
| `tests/integration/test_maintenance.py` | processed_events older than 30 days purged, newer kept; idle consumers with no pending entries removed, a consumer with pending entries and this consumer kept; `maintenance.task.completed` logged per task (C44); a second replica skips while the lock is held | PAY-608 |
| `tests/integration/test_readyz.py` | 503 when Postgres or Redis is down, when migrations aren't at head, when the consumer task has died, and while stopping | PAY-605 |
| `tests/contract/test_event_contract.py` | Every example in `contracts/events/examples/payout.requested.v1.*.json` (lead-owned files, created with S5; C57) (dev `COMPANY_ID`, C28) validates against `contracts/events/payout.requested.v1.json` and parses into `Envelope`; hypothesis-jsonschema samples from the schema parse too; invalid examples fail both | PAY-402, NFR-12 |
| `tests/contract/test_dlq_contract.py` | The DLQ field maps the worker builds for every `error_code` (including the missing-`envelope` case), decoded as the schema describes (`delivery_count` as an integer), validate against `contracts/events/payout.requested.dlq.v1.json` (C21); so does the schema's own example | PAY-408, NFR-12 |
| `tests/contract/test_provider_contracts.py` | `Instruction`, `ResultRequest`, `ResultResponse` match `services/payroll/openapi.json` (the result `outcome` enum has all five attempt statuses; `bank_reference` max 64); `TransferRequest`/`TransferResponse` match `services/bank_stub/openapi.json` (201 and 200 bodies `{transfer_id, status, reason, bank_reference}`) | PAY-405, 411, 412 |

The stateful payout model (`test_payout_model.py`, PAY-NFR-02) is owned by payroll only (C44):
`services/payroll/tests/unit/test_payout_model.py`. The worker has no copy.

**Resilience** (`tests/resilience/`, Compose `test` profile with Toxiproxy in front of Postgres, Redis, payroll and the bank; toxics reset in teardown). Each runs a 50-employee run (500 nightly) and then asserts with bank-stub's `GET /v1/transfers/{key}` and payroll's payouts API that every payout is final and each payout has at most one settled transfer.

| Test file | Fault | Expected | Requirement |
|---|---|---|---|
| `test_worker_crash.py` | `docker kill` the worker mid-run, then start it | Run `paid`/`partially_failed`; no duplicate transfers | PAY-408, NFR-02 |
| `test_bank_down.py` | Disable the bank proxy, then re-enable after the budget | Breaker opens (`circuit_breaker_state{target="bank"}` = 2); attempts `indeterminate`; run stays `processing`; after re-enable the re-publish settles them | PAY-404, 407, NFR-03 |
| `test_bank_timeout_after_settle.py` | Toxiproxy `timeout` toxic on the bank response path | Bank settles, worker sees no answer; the same-key retry or the later re-publish returns the original; ≤ 1 settled per payout | PAY-404, NFR-02 |
| `test_bank_restart.py` | Restart bank-stub mid-run | Persistent ledger: replays return the original; ≤ 1 settled per payout | PAY-412, NFR-02 |
| `test_payroll_down.py` | Disable the worker → payroll proxy past the budget, then re-enable | DLQ entries, `queue_messages_total{outcome="dlq"}` > 0 (`PayoutDLQ`), attempts stay `requested`; re-publish completes the run | PAY-408, NFR-03 |
| `test_poison.py` | XADD an invalid envelope and an amount-mismatched one (admin Redis user) | Both in the DLQ; the readable one's attempt is `needs_operator` | PAY-409 |
| `test_redis_data_loss.py` | `FLUSHALL` mid-run | NOGROUP → `queue.group.recreated`; payroll re-publishes; the run reaches a final status | PAY-407, 408, NFR-03 |

## 7. Traceability

| Requirement | Design section | Tests |
|---|---|---|
| PAY-402 (consumer side: envelope, stream fields, schema) | §2.3, §3.1 step 0 | `test_models.py`, `test_event_contract.py` |
| PAY-403 | §3.1, §3.3 | `test_handler.py`, `test_consumer.py`, `test_crash_points.py`, `test_two_consumers.py`, `test_old_attempt.py` |
| PAY-404 | §3.1 step 6, §3.5 | `test_classify.py`, `test_bank_client.py`, `test_budget.py`, `test_bank_down.py`, `test_bank_timeout_after_settle.py` |
| PAY-405 (client side; any stored outcome accepted) | §2.3, §3.1 step 7, §3.4 | `test_handler.py`, `test_payroll_client.py`, `test_republish_race.py`, `test_provider_contracts.py` |
| PAY-407 (worker side: fresh event IDs pass, NOGROUP) | §3.1 step 1, §3.3.6 | `test_republish_race.py`, `test_redis_data_loss.py` |
| PAY-408 (poison at once; payroll and DB failures at the budget; DLQ fields and MAXLEN) | §3.2, §3.3 | `test_failure_policy.py`, `test_dlq.py`, `test_dlq_contract.py`, `test_budget.py`, `test_poison.py`, `test_delivery_counts.py`, `test_nogroup.py`, `test_shutdown.py`, `test_worker_crash.py`, `test_payroll_down.py` |
| PAY-409 | §3.1 steps 0 and 4, §3.2 | `test_poison.py` (integration and resilience) |
| PAY-410 | §3.1 step 5 | `test_handler.py`, `test_key_window.py` |
| PAY-411 (client side) | §2.3, §3.4 | `test_payroll_client.py`, `test_old_attempt.py`, `test_provider_contracts.py` |
| PAY-412 (client side) | §3.5 | `test_bank_client.py`, `test_classify.py`, `test_bank_restart.py`, `test_provider_contracts.py` |
| PAY-600 | §3.6 | `test_jwt_signer.py` |
| PAY-605 | §3.7, §4 | `test_settings.py`, `test_startup_checks.py`, `test_readyz.py`; prod replicas 0: `tests/platform/test_k8s_policy.py` |
| PAY-606 | §2.1, §2.2, §4 | `test_migrations.py`, `test_grants.py` |
| PAY-607 | §3.3.1 | `test_redis_acl.py` |
| PAY-608 | §3.8 | `test_maintenance.py` |
| PAY-NFR-02 | §3.1, §3.5 | `test_crash_points.py`, `test_two_consumers.py`, resilience rows; the stateful model is payroll's `services/payroll/tests/unit/test_payout_model.py` (C44) |
| PAY-NFR-03 | §3.2, §3.3 | `test_bank_down.py`, `test_payroll_down.py`, `test_redis_data_loss.py` |
| PAY-NFR-07 | §2.3, §3.2 (DLQ `error`), §5 | `test_models.py`, `test_dlq.py`, `test_no_secrets_in_logs.py` |
| PAY-NFR-09 | §5.3 | `test_trace_context.py` (plus e2e `trace.spec.ts`) |
| PAY-NFR-10 | §5.2 | `test_metrics.py` |
| PAY-NFR-11 (`PayoutDLQ` sees the first DLQ) | §5.2 | `test_metrics.py`, `test_budget.py` |

## Change log

- 2026-11-10: incorporated README R-table and C-table decisions (R1, R2, R5, R6, R7, R9, R10, R11, R12, R13, R14, R27, R35; C1, C2, C21, C22, C23, C24, C26, C27, C28, C29, C30, C32, C34, C44).

Resolved after the change log entry: C46 (`PAYOUT_PAYROLL_*` keys), C47 (`COMPANY_ID` for every service, `PAYOUT_CONSUMER_GC_IDLE_S`, startup reason `not_deployable`). Nothing is open.
