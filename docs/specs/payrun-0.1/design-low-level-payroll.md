# Low-level design: payroll service

Status: draft v2, 2026-11-10 · Sources: [architecture plan](../../plan/architecture-plan.md) v1.2 (accepted 2026-11-10), [requirements](requirements.md), [high-level design](design-high-level.md), the [spec README](README.md) "Spec resolutions" (R1–R37) and "Contract decisions" (C1–C55) tables, ADRs [0003](../../adr/0003-payout-flow.md), [0004](../../adr/0004-bank-details-at-rest.md), [0005](../../adr/0005-four-eyes-approval.md), [0006](../../adr/0006-money-and-rounding.md) · Event contracts: [`contracts/events/payout.requested.v1.json`](../../../contracts/events/payout.requested.v1.json), [`contracts/events/payout.requested.dlq.v1.json`](../../../contracts/events/payout.requested.dlq.v1.json) (written by payout-worker)

This document is the contract between the payroll implementers, the payout-worker team (§4.6, §4.7, §5)
and the web team (§4.2–§4.5). Precedence: the README C-table, then the R-table, then `requirements.md`,
then this design. The body below already applies every R and C decision that affects payroll; §10
records the change. Choices this design makes where the sources are silent are marked **[decision]**;
the lead accepted them (R34).

Conventions used throughout:
- **IDs** are UUIDv7, generated in the application by `pycommon.db.new_id()`. The DB has no default for them.
- **Money** is `BIGINT` cents (Python `int`), **rates** are `INTEGER` basis points and **hours** are `INTEGER` hundredths. No `float` anywhere in `payroll.domain` (PAY-NFR-01).
- **Time** is `timestamptz` in UTC. **Period dates** are `date` (plan §2.1).
- **Actor IDs** (`*_by` columns, `audit_events.actor_id`) are `TEXT`. They hold the JWT `sub`: a user UUID for web tokens, `service:payout-worker` for the worker, or `system:maintenance` for the maintenance task.
- **Actor display names** (`calculated_by_name`, `approved_by_name`) come from the user token's `name` claim (`pycommon.auth.Principal.name: str | None`; R9, C10). Payroll never calls identity for them; a token without `name` stores NULL.
- Every query is scoped by the token's `cid` (PAY-601). Repositories take `company_id` as their first argument and never read it from input.

---

## 1. Modules

All paths are under `services/payroll/`. The package is `payroll`.

| Module (path) | New / changed | Responsibility |
|---|---|---|
| `src/payroll/main.py` | new | `create_app()` calls `pycommon.create_app(settings)` (OTel, request logging, 256 KB body limit, error handlers, JWT auth dependency, `/livez`, `/readyz`). It then mounts the routers and registers the lifespan tasks (outbox relay, maintenance runner) and the collection-time gauge callbacks (§7.2). |
| `src/payroll/settings.py` | new | `Settings(pycommon.settings.BaseServiceSettings)`: inherits the shared `DB_*`, `REDIS_*` and other pycommon keys (C1), and adds the payroll-only `PAYROLL_*` keys and the shared `PAYOUT_*` timing keys (§6). `STARTUP_CHECKS`, the payroll extras passed to pycommon's `run_startup_checks` (§6.2). |
| `src/payroll/crypto.py` | new | `FieldKeyring` (AES-256-GCM field encryption, kid rotation) and `FieldDecryptError` (§3.6). |
| `src/payroll/metrics.py` | new | Every OTel instrument in §7.2, plus `register_collection_gauges(sync_engine)` (the collection-time gauge callbacks, including `outbox_*`; C18). |
| `src/payroll/maintenance.py` | new | `build_maintenance(ctx) -> pycommon.maintenance.MaintenanceRunner` with lock name `payroll:maintenance` and the tasks `republish`, `purge_outbox`, `purge_idempotency_keys` and `purge_bank_details` (§5.3, §5.4). |
| `src/payroll/api/__init__.py` | new | `routers: list[APIRouter]`. |
| `src/payroll/api/deps.py` | new | `get_session`, `principal`, `require_roles(*roles)`, `require_web_user`, `require_worker`, `idempotency(required: bool)` (§4.8, §8). |
| `src/payroll/api/errors.py` | new | `ErrorCode` (a StrEnum with every code in §4.1), plus the mapping from `DomainError` subclasses to `(status, code)`. |
| `src/payroll/api/schemas.py` | new | Every Pydantic request and response model in §4. |
| `src/payroll/api/pagination.py` | new | `encode_cursor(after_id)` and `decode_cursor(cursor) -> UUID` (§4.9). |
| `src/payroll/api/settings_router.py` | new | `GET` / `PUT /v1/company/settings`. |
| `src/payroll/api/employees_router.py` | new | `/v1/employees*`. |
| `src/payroll/api/pay_runs_router.py` | new | `/v1/pay-runs*`: create, list, get, delete, hours, calculate, approve, payslips, payouts, retry and resolve. |
| `src/payroll/api/audit_router.py` | new | `GET /v1/audit-events`. |
| `src/payroll/api/internal_router.py` | new | `/v1/internal/payouts/{id}/instruction` and `/result`. |
| `src/payroll/services/` | new | Application services, one per use case (`create_run.py`, `hours.py`, `calculate.py`, `approve.py`, `retry.py`, `resolve.py`, `record_result.py`, `instruction.py`, `employees.py`, `settings.py`). Each one owns its transaction and its lock order (§4.10), calls the domain functions and the repositories, writes the audit rows, and emits the logs and metrics. Routers call only these. |
| `src/payroll/domain/__init__.py` | new | `DomainError` base class and all its subclasses (§3.7). |
| `src/payroll/domain/periods.py` | new | Period derivation, `period_index`, the overlap test and the hours bounds (§3.1). |
| `src/payroll/domain/calc.py` | new | Gross, tax, retirement, net and totals in integer arithmetic (§3.2). |
| `src/payroll/domain/state.py` | new | Status enums, `run_status_from_payouts` (PAY-406), the mutability and approvability guards, `decide_result` and `instruction_allowed` (§3.3). |
| `src/payroll/domain/dual_approval.py` | new | `check_approve_dual` and `check_retry_dual` (PAY-401, PAY-500) (§3.4). |
| `src/payroll/domain/retry.py` | new | `check_retry`, `republish_backoff_s` and `republish_decision` (PAY-500, PAY-407) (§3.5). |
| `src/payroll/domain/resolve.py` | new | `ResolveAction`, `check_resolve` and `resolve_transition` (PAY-502) (§3.5a). |
| `src/payroll/domain/validation.py` | new | `aba_checksum_ok`, `is_monday` and the IANA timezone check. |
| `src/payroll/db/models.py` | new | SQLAlchemy 2.0 declarative models for the 10 tables in §2 (`__table_args__ = {"schema": "payroll"}`). |
| `src/payroll/db/session.py` | new | The async engine from `pycommon.db.create_engine(settings)` (asyncpg; `DB_*` keys; `statement_timeout` = `DB_STATEMENT_TIMEOUT_MS`, pool timeout `DB_POOL_TIMEOUT_S`); a sync engine (psycopg, pool size 1, same `DB_*` credentials) for the collection-time gauges; and `session_scope()`. |
| `src/payroll/db/repositories.py` | new | `SettingsRepo`, `EmployeeRepo`, `PayRunRepo`, `PayslipRepo`, `HoursRepo`, `PayoutRepo`, `AttemptRepo`, `AuditRepo`, `OutboxRepo`, `IdempotencyRepo`. Every method takes `company_id` first and uses bound parameters only. |
| `src/payroll/events/envelope.py` | new | `build_payout_requested(...)` and `validate_envelope(...)` (against the committed JSON Schema) (§5.1). |
| `src/payroll/events/outbox.py` | new | `OutboxWriter.add_payout_requested(session, ...)`. It runs inside the caller's transaction and wakes the local relay after commit. |
| `src/payroll/events/relay.py` | new | `PayrollOutboxSource`, payroll's implementation of the repository protocol of pycommon's relay loop (SKIP LOCKED batches, mark sent, `last_published_at`), publishing through `pycommon.streams.StreamProducer.publish` (§5.2). |
| `src/payroll/events/republish.py` | new | `republish_due_attempts(...)` (§5.3), called by the maintenance runner. |
| `migrations/` | new | Alembic via `pycommon.db.run_alembic_env(target_metadata, schema="payroll", runtime_role="payroll_app")` (`version_table_schema="payroll"`). Revisions `0001_initial_schema` and `0002_seed_company_settings` (§2.11). |
| `openapi.json` | new | Generated by `scripts/export_openapi.py payroll` and committed. |
| `tests/{unit,integration,contract}/` | new | §9. |

Layering rules:
- `domain/` imports nothing from `api/`, `db/`, `events/` or third-party I/O libraries. It uses only `dataclasses`, `datetime`, `enum`, `uuid` and `zoneinfo`.
- `api/` does HTTP mapping only.
- `services/` is the only layer that opens transactions.

---

## 2. Data models

These are the exact DDL statements that `0001_initial_schema` emits, in Postgres 16, schema `payroll`.
- Every table has `created_at timestamptz NOT NULL DEFAULT now()` and `updated_at timestamptz NOT NULL DEFAULT now()`. The ORM sets `updated_at` on update (`onupdate=func.now()`). There are no triggers.
- Foreign keys are `ON DELETE RESTRICT`.
- Grants come from the bootstrap default privileges (`deploy/compose/init/20-payroll.sql`, PAY-606): the runtime role `payroll_app` gets DML only; the migrator `payroll_migrator` owns the schema (C2). The runtime role can read `payroll.alembic_version` but not change it.

### 2.1 `company_settings`

```sql
CREATE TABLE payroll.company_settings (
  company_id            uuid        PRIMARY KEY,
  name                  text        NOT NULL CHECK (char_length(name) BETWEEN 1 AND 200),
  tax_rate_bps          integer     NOT NULL CHECK (tax_rate_bps BETWEEN 0 AND 5000),
  retirement_bps        integer     NOT NULL CHECK (retirement_bps BETWEEN 0 AND 2000),
  timezone              text        NOT NULL DEFAULT 'America/New_York',
  biweekly_anchor       date        NOT NULL DEFAULT DATE '2026-01-05'
                                    CHECK (extract(isodow FROM biweekly_anchor) = 1),
  require_dual_approval boolean     NOT NULL DEFAULT true,
  updated_by            text        NULL,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now()
);
```

The IANA validity of `timezone` is checked in the application (`zoneinfo.ZoneInfo(tz)`), not in SQL.

### 2.2 `employees`

```sql
CREATE TABLE payroll.employees (
  id                  uuid        PRIMARY KEY,
  company_id          uuid        NOT NULL REFERENCES payroll.company_settings(company_id),
  name                text        NOT NULL CHECK (char_length(name) BETWEEN 1 AND 200),
  email               text        NOT NULL CHECK (char_length(email) BETWEEN 3 AND 320),
  pay_type            text        NOT NULL CHECK (pay_type IN ('salaried','hourly')),
  annual_salary_cents bigint      NULL,
  hourly_rate_cents   bigint      NULL,
  schedule            text        NOT NULL CHECK (schedule IN ('biweekly','monthly')),
  bank_routing_enc    text        NULL,
  bank_account_enc    text        NULL,
  account_last4       char(4)     NULL CHECK (account_last4 ~ '^[0-9]{4}$'),
  bank_updated_by     text        NULL,
  bank_updated_at     timestamptz NULL,
  bank_purged_at      timestamptz NULL,
  status              text        NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive')),
  deactivated_at      timestamptz NULL,
  created_by          text        NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_employees_pay_basis CHECK (
    (pay_type = 'salaried' AND annual_salary_cents BETWEEN 1 AND 1000000000 AND hourly_rate_cents IS NULL)
 OR (pay_type = 'hourly'   AND hourly_rate_cents   BETWEEN 1 AND 100000     AND annual_salary_cents IS NULL)),
  CONSTRAINT ck_employees_deactivated CHECK ((status = 'inactive') = (deactivated_at IS NOT NULL)),
  CONSTRAINT ck_employees_bank_all_or_none CHECK (
    (bank_routing_enc IS NULL) = (bank_account_enc IS NULL)
    AND (bank_account_enc IS NULL) = (account_last4 IS NULL)),
  CONSTRAINT ck_employees_active_has_bank CHECK (status = 'inactive' OR bank_account_enc IS NOT NULL)
);
CREATE UNIQUE INDEX uq_employees_company_email ON payroll.employees (company_id, lower(email));
CREATE INDEX ix_employees_list     ON payroll.employees (company_id, status, name, id);
CREATE INDEX ix_employees_schedule ON payroll.employees (company_id, schedule, status);
CREATE INDEX ix_employees_purge    ON payroll.employees (deactivated_at)
  WHERE status = 'inactive' AND bank_account_enc IS NOT NULL;
```

- `bank_updated_by` and `bank_updated_at` are set on create (creating an employee counts as a bank edit; R34) and on every PATCH that changes `bank_routing` or `bank_account` (PAY-203).
- The purge (§5.4) sets the three bank columns to NULL and sets `bank_purged_at`.

### 2.3 `pay_runs`

```sql
CREATE TABLE payroll.pay_runs (
  id                     uuid        PRIMARY KEY,
  company_id             uuid        NOT NULL REFERENCES payroll.company_settings(company_id),
  schedule               text        NOT NULL CHECK (schedule IN ('biweekly','monthly')),
  period_start           date        NOT NULL,
  period_end             date        NOT NULL,
  pay_date               date        NOT NULL,
  period_index           smallint    NOT NULL,
  periods_per_year       smallint    NOT NULL CHECK (periods_per_year IN (12, 26)),
  status                 text        NOT NULL DEFAULT 'draft'
                         CHECK (status IN ('draft','calculated','processing','paid','partially_failed')),
  employee_count         integer     NOT NULL CHECK (employee_count >= 1),
  tax_bps                integer     NULL CHECK (tax_bps BETWEEN 0 AND 5000),
  retirement_bps         integer     NULL CHECK (retirement_bps BETWEEN 0 AND 2000),
  require_dual_approval  boolean     NULL,
  total_gross_cents      bigint      NULL CHECK (total_gross_cents >= 0),
  total_tax_cents        bigint      NULL CHECK (total_tax_cents >= 0),
  total_retirement_cents bigint      NULL CHECK (total_retirement_cents >= 0),
  total_net_cents        bigint      NULL CHECK (total_net_cents >= 0),
  created_by             text        NOT NULL,
  calculated_by          text        NULL,
  calculated_by_name     text        NULL CHECK (char_length(calculated_by_name) <= 200),
  calculated_at          timestamptz NULL,
  approved_by            text        NULL,
  approved_by_name       text        NULL CHECK (char_length(approved_by_name) <= 200),
  approved_at            timestamptz NULL,
  processing_since       timestamptz NULL,
  first_final_at         timestamptz NULL,
  deleted_at             timestamptz NULL,
  deleted_by             text        NULL,
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_pay_runs_period CHECK (period_end >= period_start AND pay_date = period_end
                                       AND period_index BETWEEN 1 AND periods_per_year),
  CONSTRAINT ck_pay_runs_totals CHECK (
    (total_gross_cents IS NULL AND total_tax_cents IS NULL AND total_retirement_cents IS NULL AND total_net_cents IS NULL)
 OR (total_gross_cents = total_net_cents + total_tax_cents + total_retirement_cents)),
  CONSTRAINT ck_pay_runs_calculated CHECK (
    status = 'draft' OR (calculated_by IS NOT NULL AND tax_bps IS NOT NULL AND retirement_bps IS NOT NULL
                         AND require_dual_approval IS NOT NULL AND total_net_cents IS NOT NULL)),
  CONSTRAINT ck_pay_runs_approved CHECK (
    (status IN ('draft','calculated')) = (approved_at IS NULL) AND (approved_at IS NULL) = (approved_by IS NULL)),
  CONSTRAINT ck_pay_runs_processing CHECK (status <> 'processing' OR processing_since IS NOT NULL),
  CONSTRAINT ck_pay_runs_deleted CHECK (deleted_at IS NULL OR status IN ('draft','calculated'))
);
CREATE UNIQUE INDEX uq_pay_runs_company_schedule_period
  ON payroll.pay_runs (company_id, schedule, period_start) WHERE deleted_at IS NULL;
CREATE INDEX ix_pay_runs_list ON payroll.pay_runs (company_id, period_start DESC, id DESC) WHERE deleted_at IS NULL;
CREATE INDEX ix_pay_runs_processing ON payroll.pay_runs (processing_since) WHERE status = 'processing';
```

- `first_final_at` (R13) is set the first time the run reaches `paid` or `partially_failed`, and from then on it guards the single observation of `payroll_payout_settlement_seconds` (PAY-406).
- `calculated_by_name` and `approved_by_name` (R9, C9) are copied from the token's `name` claim at calculate and approve, for display only. They are NULL when the claim is absent, and `calculated_by_name` is cleared with `calculated_by` when hours revert the run to `draft`. They are the only personal data on the row (plan §6: staff names).
- `approved` is an instant, not a status (plan §2.1).

### 2.4 `pay_run_hours`

```sql
CREATE TABLE payroll.pay_run_hours (
  id               uuid        PRIMARY KEY,
  company_id       uuid        NOT NULL,
  pay_run_id       uuid        NOT NULL,
  employee_id      uuid        NOT NULL,
  hours_hundredths integer     NOT NULL CHECK (hours_hundredths BETWEEN 0 AND 74400),
  entered_by       text        NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_pay_run_hours UNIQUE (pay_run_id, employee_id),
  CONSTRAINT fk_pay_run_hours_payslip FOREIGN KEY (pay_run_id, employee_id)
    REFERENCES payroll.payslips (pay_run_id, employee_id)
);
```

- The schedule-specific bound (33,600 biweekly, 74,400 monthly) is enforced in `domain.periods.max_hours_hundredths`. The CHECK is the outer bound.
- The table is created after `payslips`, so that the FK can reference it.

### 2.5 `payslips`

```sql
CREATE TABLE payroll.payslips (
  id                  uuid        PRIMARY KEY,
  company_id          uuid        NOT NULL,
  pay_run_id          uuid        NOT NULL REFERENCES payroll.pay_runs(id),
  employee_id         uuid        NOT NULL REFERENCES payroll.employees(id),
  employee_name       text        NOT NULL,
  pay_type            text        NOT NULL CHECK (pay_type IN ('salaried','hourly')),
  annual_salary_cents bigint      NULL,
  hourly_rate_cents   bigint      NULL,
  hours_hundredths    integer     NULL,
  status              text        NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','calculated')),
  gross_cents         bigint      NULL CHECK (gross_cents >= 0),
  tax_cents           bigint      NULL CHECK (tax_cents >= 0),
  retirement_cents    bigint      NULL CHECK (retirement_cents >= 0),
  net_cents           bigint      NULL CHECK (net_cents >= 0),
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_payslips_run_employee UNIQUE (pay_run_id, employee_id),
  CONSTRAINT ck_payslips_basis CHECK (
    (pay_type = 'salaried' AND annual_salary_cents IS NOT NULL AND hourly_rate_cents IS NULL AND hours_hundredths IS NULL)
 OR (pay_type = 'hourly'   AND hourly_rate_cents IS NOT NULL AND annual_salary_cents IS NULL)),
  CONSTRAINT ck_payslips_amounts CHECK (
    (status = 'pending' AND gross_cents IS NULL AND tax_cents IS NULL AND retirement_cents IS NULL AND net_cents IS NULL)
 OR (status = 'calculated' AND gross_cents = net_cents + tax_cents + retirement_cents))
);
CREATE INDEX ix_payslips_list     ON payroll.payslips (pay_run_id, employee_name, id);
CREATE INDEX ix_payslips_employee ON payroll.payslips (employee_id, pay_run_id);
```

`hours_hundredths` is copied from `pay_run_hours` at calculate time, so a payslip can be reproduced from its own row.

### 2.6 `payouts`

```sql
CREATE TABLE payroll.payouts (
  id              uuid        PRIMARY KEY,
  company_id      uuid        NOT NULL,
  pay_run_id      uuid        NOT NULL REFERENCES payroll.pay_runs(id),
  payslip_id      uuid        NOT NULL UNIQUE REFERENCES payroll.payslips(id),
  employee_id     uuid        NOT NULL REFERENCES payroll.employees(id),
  amount_cents    bigint      NOT NULL CHECK (amount_cents BETWEEN 0 AND 1000000000),
  currency        char(3)     NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
  status          text        NOT NULL CHECK (status IN ('requested','paid','failed')),
  no_transfer     boolean     NOT NULL DEFAULT false,
  current_attempt smallint    NOT NULL,
  final_at        timestamptz NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_payouts_no_transfer CHECK (no_transfer = (amount_cents = 0)),
  CONSTRAINT ck_payouts_no_transfer_state CHECK (
    (no_transfer AND status = 'paid' AND current_attempt = 0)
 OR (NOT no_transfer AND current_attempt >= 1)),
  CONSTRAINT ck_payouts_final CHECK ((status IN ('paid','failed')) = (final_at IS NOT NULL))
);
CREATE INDEX ix_payouts_run_status ON payroll.payouts (pay_run_id, status);
CREATE INDEX ix_payouts_employee   ON payroll.payouts (employee_id);
```

`final_at` is the time the payout most recently became final. A retry clears it.

### 2.7 `payout_attempts`

```sql
CREATE TABLE payroll.payout_attempts (
  id                uuid        PRIMARY KEY,
  company_id        uuid        NOT NULL,
  payout_id         uuid        NOT NULL REFERENCES payroll.payouts(id),
  attempt           smallint    NOT NULL CHECK (attempt >= 1),
  status            text        NOT NULL DEFAULT 'requested'
                    CHECK (status IN ('requested','indeterminate','needs_operator','settled','rejected')),
  failure_reason    text        NULL CHECK (failure_reason ~ '^[a-z0-9_]{1,100}$'),
  bank_reference    text        NULL CHECK (char_length(bank_reference) BETWEEN 1 AND 64),
  bank_snapshot_enc text        NULL,
  account_last4     char(4)     NOT NULL CHECK (account_last4 ~ '^[0-9]{4}$'),
  requested_by      text        NOT NULL,
  requested_at      timestamptz NOT NULL,
  last_published_at timestamptz NOT NULL,
  needs_operator_at timestamptz NULL,       -- set when the attempt enters needs_operator, cleared by redrive (C48)
  CONSTRAINT ck_attempt_needs_operator_at CHECK ((status = 'needs_operator') = (needs_operator_at IS NOT NULL)),
  publish_count     integer     NOT NULL DEFAULT 1 CHECK (publish_count >= 1),
  final_at          timestamptz NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_payout_attempts_payout_attempt UNIQUE (payout_id, attempt),
  CONSTRAINT ck_attempts_final CHECK ((status IN ('settled','rejected')) = (final_at IS NOT NULL)),
  CONSTRAINT ck_attempts_rejected_reason CHECK (status <> 'rejected' OR failure_reason IS NOT NULL),
  CONSTRAINT ck_attempts_settled_ref CHECK (status <> 'settled' OR bank_reference IS NOT NULL),
  CONSTRAINT ck_attempts_snapshot CHECK (status IN ('settled','rejected') OR bank_snapshot_enc IS NOT NULL)
);
CREATE INDEX ix_attempts_republish ON payroll.payout_attempts (last_published_at)
  WHERE status IN ('requested','indeterminate');
CREATE INDEX ix_attempts_nonfinal ON payroll.payout_attempts (requested_at)
  WHERE status IN ('requested','indeterminate','needs_operator');
```

- `ck_attempts_snapshot` lets the purge (PAY-608) null snapshots only on final attempts.
- `account_last4` is a display copy, so the attempt history can show which account each attempt paid (J5) **[decision]**.
- `bank_reference` is at most 64 characters, the same bound as the worker and bank-stub (C26).
- **Meaning of `publish_count`:** how many times this attempt has been published since it was created or last redriven, counting that publication. So the number of re-publishes so far is `publish_count − 1` (§3.5). An operator redrive (PAY-502) sets it back to 1 (C13).
- `failure_reason` holds the bank's reason for `rejected`, `operator_rejected` for an operator's mark_rejected (PAY-502), and the cause for `needs_operator` (`republish_limit`, or the worker's poison reason such as `schema_invalid`, `amount_mismatch`, `key_window_expired`). A redrive clears it.

### 2.8 `audit_events`

```sql
CREATE TABLE payroll.audit_events (
  id          uuid        PRIMARY KEY,
  company_id  uuid        NOT NULL,
  actor_id    text        NOT NULL,
  event       text        NOT NULL CHECK (event ~ '^[a-z_]+\.[a-z_]+$'),
  entity_type text        NOT NULL CHECK (entity_type IN ('company_settings','employee','pay_run','payout')),
  entity_id   uuid        NOT NULL,
  details     jsonb       NOT NULL DEFAULT '{}'::jsonb,
  request_id  text        NULL,
  trace_id    text        NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_events_list   ON payroll.audit_events (company_id, id DESC);
CREATE INDEX ix_audit_events_entity ON payroll.audit_events (company_id, entity_type, entity_id, id DESC);
```

Audit events and the shape of `details`:

| Event | Details | Requirement |
|---|---|---|
| `settings.updated` | `{changed: {field: {old, new}}}` (no personal data) | PAY-200 |
| `employee.created` | `{fields: [names], account_last4}` | PAY-201 |
| `employee.updated` | `{fields: [names]}`, plus `{account_last4: {old, new}}` on bank changes, plus `{status: {old, new}}` | PAY-203 |
| `employee.bank_details_purged` | `{deactivated_at, attempts_nulled: n}` | PAY-608 |
| `payrun.created` | `{schedule, period_start, employee_count}` | PAY-300 |
| `payrun.hours_updated` | `{entries: n, reverted_to_draft: bool}` | PAY-301 |
| `payrun.calculated` | `{tax_bps, retirement_bps, require_dual_approval, payslips: n}` | PAY-302 |
| `payrun.approved` | `{payouts: n, transfers: n, no_transfer: n, status}` | PAY-400 |
| `payrun.deleted` | `{status_at_delete}` | PAY-304 |
| `payout.result_recorded` | `{attempt, outcome, applied: bool, failure_reason?}` | PAY-405 |
| `payout.retry_requested` | `{attempt, account_last4}` | PAY-500 |
| `payout.needs_operator` | `{attempt, reason}` (`poison`, `republish_limit` or the worker's reason) | PAY-407, PAY-409 |
| `payout.operator_resolved` | `{attempt, action, note_len}` (`action` is `redrive` or `mark_rejected`; the note's length, never its text) | PAY-502 |

No money amounts, names, emails or bank numbers other than `account_last4` appear in `details` (plan §6).

### 2.9 `outbox`

```sql
CREATE TABLE payroll.outbox (
  id              uuid        PRIMARY KEY,          -- = envelope.event_id
  company_id      uuid        NOT NULL,
  event_type      text        NOT NULL CHECK (event_type = 'payout.requested'),
  event_version   smallint    NOT NULL CHECK (event_version = 1),
  payload         jsonb       NOT NULL,             -- the full envelope (§5.1)
  payout_id       uuid        NULL,
  attempt         smallint    NULL,
  sent_at         timestamptz NULL,
  relay_attempts  integer     NOT NULL DEFAULT 0,
  last_error      text        NULL,                 -- exception class name only, never a message
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_outbox_unsent         ON payroll.outbox (created_at, id) WHERE sent_at IS NULL;
CREATE INDEX ix_outbox_unsent_attempt ON payroll.outbox (payout_id, attempt) WHERE sent_at IS NULL;
CREATE INDEX ix_outbox_sent           ON payroll.outbox (sent_at) WHERE sent_at IS NOT NULL;
```

### 2.10 `idempotency_keys`

```sql
CREATE TABLE payroll.idempotency_keys (
  company_id      uuid        NOT NULL,
  scope           text        NOT NULL,   -- "<iss>|<sub>|<METHOD> <route template>|<path params>"
  key             text        NOT NULL CHECK (key ~ '^[A-Za-z0-9._:-]{1,200}$'),
  request_hash    char(64)    NOT NULL,   -- hex sha256 (§4.8)
  response_status smallint    NOT NULL,
  response_body   jsonb       NOT NULL,
  expires_at      timestamptz NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (company_id, scope, key)
);
CREATE INDEX ix_idempotency_expires ON payroll.idempotency_keys (expires_at);
```

### 2.11 Migrations

- **`0001_initial_schema`** creates the tables in this order: `company_settings`, `employees`, `pay_runs`, `payslips`, `pay_run_hours`, `payouts`, `payout_attempts`, `audit_events`, `outbox`, `idempotency_keys`, plus every index above. Its downgrade drops them in reverse.
- **`0002_seed_company_settings`** (data migration, PAY-200, plan §8 "first owner"):

  ```python
  def upgrade() -> None:
      company_id = os.environ["COMPANY_ID"]                  # KeyError → the migration fails loudly
      uuid.UUID(company_id)                                   # validates the format
      name = os.environ.get("PAYROLL_SEED_COMPANY_NAME", "PayRun Demo Company")
      op.execute(sa.text(
          "INSERT INTO payroll.company_settings (company_id, name, tax_rate_bps, retirement_bps) "
          "VALUES (:cid, :name, 0, 0) ON CONFLICT (company_id) DO NOTHING"
      ).bindparams(cid=company_id, name=name))
      # timezone, biweekly_anchor and require_dual_approval take their column defaults
  def downgrade() -> None:
      op.execute(sa.text("DELETE FROM payroll.company_settings c WHERE c.company_id = :cid "
                         "AND NOT EXISTS (SELECT 1 FROM payroll.employees e WHERE e.company_id = c.company_id)")
                 .bindparams(cid=os.environ["COMPANY_ID"]))
  ```

  - The migration seeds `tax_rate_bps` = 0 and `retirement_bps` = 0 and the name from `PAYROLL_SEED_COMPANY_NAME` (R22). `scripts/seed.py` then sets tax 2000 bps and retirement 500 bps through `PUT /v1/company/settings` (PAY-609).
  - In dev and CI `COMPANY_ID` is `0193a000-0000-7000-8000-000000000001` (C28).
  - The migration is idempotent: re-running it keeps the existing row.
- Migrations run as `payroll_migrator` in the `migrate-payroll` one-shot or Job, before the service starts. That container sets the same pycommon keys as the service with migrator credentials: `DB_USER=payroll_migrator`, `DB_PASSWORD=${PAYROLL_DB_MIGRATOR_PASSWORD}`, `DB_SCHEMA=payroll` (C1). After `upgrade`, `run_alembic_env` revokes write access on `payroll.alembic_version` from `payroll_app`. `/readyz` checks that `alembic_version` (in schema `payroll`) is at head.

### 2.12 Run and payout state at a glance

| Entity | Statuses | Final |
|---|---|---|
| Run | `draft`, `calculated`, `processing`, `paid`, `partially_failed` | `paid`, `partially_failed` (re-openable by a retry) |
| Payslip | `pending`, `calculated` | — |
| Payout | `requested`, `paid`, `failed` | `paid`, `failed` (re-openable by a retry) |
| Attempt | `requested`, `indeterminate`, `needs_operator`, `settled`, `rejected` | `settled`, `rejected` |

Attempt transitions: `requested` → `indeterminate` \| `needs_operator` \| `settled` \| `rejected`; `indeterminate` → `needs_operator` \| `settled` \| `rejected`; `needs_operator` → `settled` \| `rejected` (a late definite bank answer, §3.3), → `requested` (operator redrive, PAY-502) or → `rejected` (operator mark_rejected, PAY-502). A final attempt never changes.

---

## 3. Domain functions

All of these are pure, synchronous and I/O-free, with full type hints (`mypy --strict`).

### 3.1 `domain/periods.py` (PAY-300, plan §2.1, ADR 0006)

```python
class Schedule(StrEnum):
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"

PERIODS_PER_YEAR: Final[Mapping[Schedule, int]] = {Schedule.BIWEEKLY: 26, Schedule.MONTHLY: 12}
MAX_HOURS_HUNDREDTHS: Final[Mapping[Schedule, int]] = {Schedule.BIWEEKLY: 33_600, Schedule.MONTHLY: 74_400}

@dataclass(frozen=True, slots=True)
class Period:
    schedule: Schedule
    period_start: date
    period_end: date
    pay_date: date
    period_index: int        # 1..periods_per_year
    periods_per_year: int

def derive_period(schedule: Schedule, period_start: date, biweekly_anchor: date) -> Period: ...
def biweekly_period_index(period_start: date, biweekly_anchor: date) -> int: ...
def max_hours_hundredths(schedule: Schedule) -> int: ...
def periods_overlap(a_start: date, a_end: date, b_start: date, b_end: date) -> bool: ...

@dataclass(frozen=True, slots=True)
class Coverage:
    employee_id: UUID
    period_start: date
    period_end: date

def find_overlapping_employees(candidates: Iterable[UUID], existing: Iterable[Coverage],
                               new_start: date, new_end: date) -> list[UUID]: ...
```

Exact rules:
- **Monthly.** Raise `InvalidPeriodStart` unless `period_start.day == 1`.
  - `period_end = date(y, m, calendar.monthrange(y, m)[1])`
  - `period_index = m`
  - `periods_per_year = 12`
- **Biweekly.** Let `delta = (period_start - biweekly_anchor).days`. Raise `InvalidPeriodStart` unless `delta % 14 == 0`. Python's `%` is non-negative for a positive modulus, so dates before the anchor work too.
  - `period_end = period_start + timedelta(days=13)`
  - `period_index = (delta // 14) % 26 + 1`
  - `periods_per_year = 26`
- **Both schedules:** `pay_date = period_end`.
- **`periods_overlap`** is an inclusive date-range overlap: `a_start <= b_end and b_start <= a_end`.
- **`find_overlapping_employees`** returns the sorted, de-duplicated IDs of candidates with any `Coverage` overlapping `[new_start, new_end]`. The repository supplies `existing` from payslips joined to non-deleted runs, already filtered by employee and date in SQL. The domain function re-checks the overlap, so the rule is unit-tested.

### 3.2 `domain/calc.py` (PAY-302, ADR 0006)

```python
class PayType(StrEnum):
    SALARIED = "salaried"
    HOURLY = "hourly"

def salaried_gross(annual_cents: int, k: int, n: int) -> int:
    base, rem = divmod(annual_cents, n)          # base = annual // n, rem = annual % n
    return base + (1 if k > n - rem else 0)

def hourly_gross(rate_cents: int, hours_hundredths: int) -> int:
    return (rate_cents * hours_hundredths + 50) // 100

def apply_bps(amount_cents: int, bps: int) -> int:
    return (amount_cents * bps + 5000) // 10000   # tax and retirement both use this

@dataclass(frozen=True, slots=True)
class PayslipInput:
    payslip_id: UUID
    employee_id: UUID
    pay_type: PayType
    annual_salary_cents: int | None
    hourly_rate_cents: int | None
    hours_hundredths: int | None            # None = no hours entered

@dataclass(frozen=True, slots=True)
class PayslipAmounts:
    gross_cents: int
    tax_cents: int
    retirement_cents: int
    net_cents: int                          # = gross - tax - retirement

@dataclass(frozen=True, slots=True)
class RunTotals:
    gross_cents: int
    tax_cents: int
    retirement_cents: int
    net_cents: int                          # funding_required_cents == net_cents

def calculate_payslip(inp: PayslipInput, *, period_index: int, periods_per_year: int,
                      tax_bps: int, retirement_bps: int) -> PayslipAmounts: ...
def calculate_run(inputs: Sequence[PayslipInput], *, period_index: int, periods_per_year: int,
                  tax_bps: int, retirement_bps: int) -> dict[UUID, PayslipAmounts]: ...
def run_totals(amounts: Iterable[PayslipAmounts]) -> RunTotals: ...
```

- Preconditions (`ValueError`, not user errors): `1 <= k <= n`, `n in (12, 26)`, `0 <= tax_bps <= 5000` and `0 <= retirement_bps <= 2000`.
- `net_cents = gross - apply_bps(gross, tax_bps) - apply_bps(gross, retirement_bps)`. Net ≥ 0 because tax + retirement ≤ 7000 bps, and the half-up rounding of each adds at most 0.5 cents. For gross ≥ 1, `apply_bps(g, 5000) + apply_bps(g, 2000) ≤ g`; the hypothesis property in §9 proves it.
- `calculate_run` first collects every hourly input with `hours_hundredths is None` and raises `HoursMissing(employee_ids=sorted(...))` before computing anything.
- `run_totals` sums the stored values. The caller passes the amounts it is about to persist, so totals = Σ stored values (PAY-302).

### 3.3 `domain/state.py` (PAY-405, PAY-406, PAY-411)

```python
class RunStatus(StrEnum):
    DRAFT = "draft"; CALCULATED = "calculated"; PROCESSING = "processing"
    PAID = "paid"; PARTIALLY_FAILED = "partially_failed"

class PayoutStatus(StrEnum):
    REQUESTED = "requested"; PAID = "paid"; FAILED = "failed"

class AttemptStatus(StrEnum):
    REQUESTED = "requested"; INDETERMINATE = "indeterminate"; NEEDS_OPERATOR = "needs_operator"
    SETTLED = "settled"; REJECTED = "rejected"

class ResultOutcome(StrEnum):
    SETTLED = "settled"; REJECTED = "rejected"; INDETERMINATE = "indeterminate"; POISON = "poison"

FINAL_ATTEMPT: Final = frozenset({AttemptStatus.SETTLED, AttemptStatus.REJECTED})
LIVE_ATTEMPT: Final = frozenset({AttemptStatus.REQUESTED, AttemptStatus.INDETERMINATE})   # deliverable
MUTABLE_RUN: Final = frozenset({RunStatus.DRAFT, RunStatus.CALCULATED})

def run_status_from_payouts(statuses: Iterable[PayoutStatus]) -> RunStatus:
    s = list(statuses)
    if not s:
        raise ValueError("an approved run has at least one payout")
    if any(x is PayoutStatus.REQUESTED for x in s):
        return RunStatus.PROCESSING
    if all(x is PayoutStatus.PAID for x in s):
        return RunStatus.PAID
    return RunStatus.PARTIALLY_FAILED

def payout_status_for_attempt(a: AttemptStatus) -> PayoutStatus:
    # settled → paid; rejected → failed; requested | indeterminate | needs_operator → requested
    ...

def assert_run_mutable(status: RunStatus) -> None: ...      # raises PayRunLocked unless draft|calculated
def assert_run_approvable(status: RunStatus) -> None: ...   # raises PayRunNotApprovable unless calculated

def instruction_allowed(*, current_attempt: int, attempt: int, attempt_status: AttemptStatus | None) -> bool:
    return attempt == current_attempt and attempt_status in LIVE_ATTEMPT

class ResultKind(StrEnum):
    APPLY = "apply"; STORED = "stored"; NOT_CURRENT = "not_current"

@dataclass(frozen=True, slots=True)
class ResultDecision:
    kind: ResultKind
    new_status: AttemptStatus | None      # set only when kind == APPLY

def decide_result(*, current_attempt: int, attempt: int, attempt_status: AttemptStatus | None,
                  outcome: ResultOutcome) -> ResultDecision: ...
```

The `decide_result` decision table (PAY-405, ADR 0003 c):

| Condition | `settled` | `rejected` | `indeterminate` | `poison` |
|---|---|---|---|---|
| `attempt != current_attempt`, or the attempt row is missing | NOT_CURRENT (409) | NOT_CURRENT | NOT_CURRENT | NOT_CURRENT |
| status `settled` or `rejected` (final) | STORED (200) | STORED | STORED | STORED |
| status `requested` or `indeterminate` | APPLY → `settled` | APPLY → `rejected` | APPLY → `indeterminate` | APPLY → `needs_operator` |
| status `needs_operator` | APPLY → `settled` | APPLY → `rejected` | STORED | STORED |

The `needs_operator` row is R7 and PAY-405: a definite bank answer for that attempt's key is applied
because the bank is authoritative. Ignoring a real `settled` would leave money paid but recorded as
unpaid.

### 3.4 `domain/dual_approval.py` (PAY-401, PAY-500, ADR 0005)

```python
@dataclass(frozen=True, slots=True)
class BankEdit:
    employee_id: UUID
    bank_updated_by: str | None
    bank_updated_at: datetime | None

class DualApprovalRule(StrEnum):
    CALCULATOR = "calculator"
    BANK_EDITOR = "bank_editor"

def check_approve_dual(*, require_dual_approval: bool, approver_id: str, calculated_by: str,
                       run_created_at: datetime, bank_edits: Iterable[BankEdit]) -> None:
    if not require_dual_approval:
        return
    if approver_id == calculated_by:
        raise ApproverMustDiffer(rule=DualApprovalRule.CALCULATOR, employee_ids=[])
    hits = sorted(e.employee_id for e in bank_edits
                  if e.bank_updated_by == approver_id
                  and e.bank_updated_at is not None and e.bank_updated_at > run_created_at)
    if hits:
        raise ApproverMustDiffer(rule=DualApprovalRule.BANK_EDITOR, employee_ids=hits)

def check_retry_dual(*, require_dual_approval: bool, retrier_id: str, bank_updated_by: str | None,
                     employee_id: UUID) -> None:
    if require_dual_approval and bank_updated_by == retrier_id:
        raise ApproverMustDiffer(rule=DualApprovalRule.BANK_EDITOR, employee_ids=[employee_id])
```

- `require_dual_approval` for approve is the run's snapshot (`pay_runs.require_dual_approval`, taken at calculate).
- For retry it is also the run's snapshot (R34).

### 3.5 `domain/retry.py` (PAY-500, PAY-407)

```python
def check_retry(*, payout_status: PayoutStatus, attempt_status: AttemptStatus, current_attempt: int,
                max_attempts: int, employee_active: bool, employee_has_bank: bool) -> None:
    if payout_status is not PayoutStatus.FAILED or attempt_status is not AttemptStatus.REJECTED:
        raise PayoutNotRetryable()                 # covers indeterminate and needs_operator attempts
    if current_attempt >= max_attempts:            # 5 including the first: refused at current_attempt = 5
        raise RetryLimitReached()
    if not (employee_active and employee_has_bank):
        raise EmployeeBankDetailsMissing(employee_ids=[...])
    # the caller then runs check_retry_dual(...)

def republish_backoff_s(publish_count: int, *, after_s: int, max_backoff_s: int) -> int:
    return min(after_s * 2 ** (publish_count - 1), max_backoff_s)

class RepublishAction(StrEnum):
    SKIP = "skip"; REPUBLISH = "republish"; NEEDS_OPERATOR = "needs_operator"

def republish_decision(*, status: AttemptStatus, is_current: bool, publish_count: int,
                       last_published_at: datetime, now: datetime, has_unsent_outbox: bool,
                       after_s: int, max_backoff_s: int, max_republish: int) -> RepublishAction:
    if status not in LIVE_ATTEMPT or not is_current or has_unsent_outbox:
        return RepublishAction.SKIP
    due = (now - last_published_at).total_seconds() > republish_backoff_s(
        publish_count, after_s=after_s, max_backoff_s=max_backoff_s)
    if not due:
        return RepublishAction.SKIP
    if publish_count - 1 >= max_republish:         # 10 re-publishes already made
        return RepublishAction.NEEDS_OPERATOR
    return RepublishAction.REPUBLISH
```

- The check order (not retryable → limit → bank details → dual approval) is fixed. Tests assert it.
- **Backoff with the defaults** (`PAYOUT_REPUBLISH_AFTER_S` = 360, R1; cap 900): with `publish_count` = 1, 2, 3… the waits are 360, 720, 900, 900… s.
  - `republish_seq` is `publish_count` **before** the increment (R34): the first publication carries 0 and the first re-publish 1.
  - The attempt becomes `needs_operator` when the 12th publication would be due, that is after 10 re-publishes. That is 360 + 720 + 9 × 900 = 9180 s (about 2.55 h, plus up to one maintenance interval per step) after the first publication at the defaults.

### 3.5a `domain/resolve.py` (PAY-502, R8, C13)

```python
class ResolveAction(StrEnum):
    REDRIVE = "redrive"; MARK_REJECTED = "mark_rejected"

OPERATOR_REJECTED: Final = "operator_rejected"

def check_resolve(*, payout_status: PayoutStatus, attempt_status: AttemptStatus) -> None:
    if payout_status is not PayoutStatus.REQUESTED or attempt_status is not AttemptStatus.NEEDS_OPERATOR:
        raise PayoutNotBlocked(attempt_status=attempt_status)

@dataclass(frozen=True, slots=True)
class ResolveTransition:
    attempt_status: AttemptStatus          # REQUESTED (redrive) or REJECTED (mark_rejected)
    failure_reason: str | None             # None (redrive clears it) or "operator_rejected"
    attempt_final: bool                    # True for mark_rejected: set final_at
    payout_status: PayoutStatus            # REQUESTED (unchanged) or FAILED
    publish: bool                          # True for redrive: write an outbox row, republish_seq = 0
    publish_count: int | None              # 1 for redrive (C13); None = unchanged

def resolve_transition(action: ResolveAction) -> ResolveTransition: ...
```

- **redrive:** attempt → `requested`, `failure_reason = NULL`, `publish_count = 1`, `last_published_at = now()` (C13); `requested_at` is **not** changed, because it dates the bank key for the key safe window (PAY-410). A new outbox row with a fresh `event_id` and `republish_seq = 0`. The payout stays `requested`. The same bank key `<payout_id>:<attempt>` is used, so a redrive can't pay twice.
- **mark_rejected:** allowed only when `now() − needs_operator_at ≥ PAYOUT_MARK_REJECTED_MIN_WAIT_S` (120 s, C48); earlier gives `ResolveTooEarly` → 409 `resolve_too_early` with `Retry-After`. Then attempt → `rejected`, `failure_reason = "operator_rejected"`, `final_at = now()`; payout → `failed`, `final_at = now()`. The normal retry (PAY-500) is then open, subject to `check_retry`.
- Either action is followed by `run_status_from_payouts` (PAY-406).
- `check_resolve` runs after the locks are taken (§4.10). Any other state → `PayoutNotBlocked` (409 `payout_not_blocked`).

### 3.6 `crypto.py` (PAY-204, ADR 0004)

```python
class FieldDecryptError(Exception):
    def __init__(self, kid: str | None) -> None: ...      # never carries plaintext or ciphertext

class FieldKeyring:
    def __init__(self, keys: Mapping[str, bytes], active_kid: str) -> None: ...
    @classmethod
    def from_settings(cls, keys_json: SecretStr, active_kid: str) -> "FieldKeyring": ...
    @property
    def active_kid(self) -> str: ...
    def encrypt(self, plaintext: str, *, aad: str) -> str: ...
    def decrypt(self, token: str, *, aad: str) -> str: ...
    def kid_of(self, token: str) -> str: ...

def employee_aad(field: Literal["bank_routing", "bank_account"], employee_id: UUID) -> str:
    return f"payroll.employees.{field}:{employee_id}"

def snapshot_aad(payout_id: UUID, attempt: int) -> str:
    return f"payroll.payout_attempts.bank_snapshot:{payout_id}:{attempt}"
```

- **Token format:** `"{kid}:{b64(nonce)}:{b64(ciphertext||tag)}"`, using standard base64 with padding. The nonce is 12 random bytes from `os.urandom`, generated per encryption. The cipher is AES-256-GCM (`cryptography.hazmat.primitives.ciphers.aead.AESGCM`), with the AAD encoded as UTF-8.
- **AAD [decision]:** binding ciphertexts to their row and field stops a ciphertext from being swapped between employees or attempts.
- **`PAYROLL_FIELD_KEYS`** is JSON `{kid: base64(32 bytes)}`. Kids match `^[A-Za-z0-9_-]{1,32}$`, so a kid never contains `:`. A key that isn't exactly 32 bytes refuses startup.
- **`decrypt`** splits on the first two `:`, looks the kid up in the keyring (so old kids keep working after rotation), and decrypts. Any failure raises `FieldDecryptError(kid)`: an unknown kid, a bad format, a base64 error or `InvalidTag`.
- **Snapshot plaintext** is compact JSON `{"routing":"<9 digits>","account":"<4–17 digits>"}`, encrypted with `snapshot_aad`.
- **Handling a decrypt error:** callers catch `FieldDecryptError`, log `crypto.decrypt_failed` at error level (fields: `kid`, `entity`, `entity_id`) and raise `InternalError`, which becomes 500 `internal_error` (PAY-204).

### 3.7 Domain errors (`domain/__init__.py`)

| Class | HTTP | Code | `details` |
|---|---|---|---|
| `InvalidPeriodStart` | 422 | `invalid_period_start` | `{schedule, period_start}` |
| `PayRunExists` | 409 | `pay_run_exists` | `{pay_run_id}` |
| `NoEmployees` | 409 | `no_employees` | — |
| `EmployeeInOverlappingRun` | 409 | `employee_in_overlapping_run` | `{employee_ids: [...]}` |
| `PayRunLocked` | 409 | `pay_run_locked` | `{status}` |
| `NotHourly` | 422 | `not_hourly` | `{employee_ids: [...]}` |
| `EmployeeNotInRun` | 422 | `employee_not_in_run` | `{employee_ids: [...]}` |
| `HoursOutOfBounds` | 422 | `validation_error` | `[{field: "body.entries.<i>.hours_hundredths", issue: "out_of_range"}]` (C14) |
| `HoursMissing` | 422 | `hours_missing` | `{employee_ids: [...]}` |
| `PayRunNotApprovable` | 409 | `pay_run_not_approvable` | `{status}` |
| `ApproverMustDiffer` | 403 | `approver_must_differ` | `{rule, employee_ids}` |
| `EmployeeBankDetailsMissing` | 409 | `employee_bank_details_missing` | `{employee_ids: [...]}` |
| `PayoutNotRetryable` | 409 | `payout_not_retryable` | `{payout_status, attempt_status}` |
| `RetryLimitReached` | 409 | `retry_limit_reached` | `{current_attempt, max_attempts}` |
| `AttemptNotCurrent` | 409 | `attempt_not_current` | `{current_attempt}` |
| `PayoutNotBlocked` | 409 | `payout_not_blocked` | `{attempt_status}` |
| `ResolveTooEarly` | 409 | `resolve_too_early` | `{retry_after_s}`; the response also sets `Retry-After` (C48) |
| `AnchorLocked` | 409 | `anchor_locked` | — |
| `EmployeeEmailTaken` | 409 | `employee_email_taken` | — |
| `NotFound(code)` | 404 | `employee_not_found`, `pay_run_not_found`, `payslip_not_found`, `payout_not_found`, `company_settings_not_found` | — |

---

## 4. Interfaces

### 4.1 Common rules

- **Base path and token:** every route is under `/v1`. Every request needs `Authorization: Bearer <internal JWT>` with `aud=payroll`, verified by pycommon against `INTERNAL_JWT_PUBLIC_KEYS` (issuer-bound kids, 5 s leeway; PAY-600).
- **Error body (PAY-602):** `{"error": {"code": str, "message": str, "request_id": str, "details"?: ...}}`.
  - Validation errors are 422 with code `validation_error` (R3) and `details` a **list** `[{"field": "<loc joined by '.'>", "issue": "<pydantic error type or a payroll issue code>"}]` (C14), for example `[{"field": "body.bank_routing", "issue": "aba_checksum"}]`. User input values are never echoed back.
  - Domain errors carry the `details` object of §3.7 (for example `{employee_ids: [...]}`).
- **Error codes on every route:**

  | Status | Code | When |
  |---|---|---|
  | 400 | `idempotency_key_required` | `Idempotency-Key` header absent on a route that requires it (C34, §4.8) |
  | 401 | `invalid_token` | Missing, malformed, expired, wrong aud, unknown kid, bad signature, kid-issuer mismatch (PAY-600), or an issuer not accepted by the route class (§8.2; PAY-411, R34) |
  | 403 | `forbidden_role` | A valid token of the wrong kind for the route (for example `sub=service:web` on a user route), or a missing role (§8.1; C30) |
  | 404 / 405 | `not_found` / `method_not_allowed` | Unknown path or method (pycommon handler) |
  | 413 | `payload_too_large` | Body > 262,144 bytes (PAY-604) |
  | 422 | `validation_error` | Pydantic or bounds failure, a malformed `Idempotency-Key` (C34), or a bad cursor (§4.9) |
  | 429 | `rate_limited` | Per-caller limit (PAY-603), with `Retry-After` |
  | 500 | `internal_error` | Unhandled, or `FieldDecryptError` |
  | 503 | `db_timeout` / `db_unavailable` | Statement or pool timeout / connection failure |

- **Full code list** (`api.errors.ErrorCode`): `invalid_token`, `forbidden_role`, `not_found`, `method_not_allowed`, `payload_too_large`, `validation_error`, `rate_limited`, `internal_error`, `db_timeout`, `db_unavailable`, `company_settings_not_found`, `anchor_locked`, `employee_not_found`, `employee_email_taken`, `pay_run_not_found`, `pay_run_exists`, `invalid_period_start`, `no_employees`, `employee_in_overlapping_run`, `pay_run_locked`, `not_hourly`, `employee_not_in_run`, `hours_missing`, `pay_run_not_approvable`, `approver_must_differ`, `idempotency_key_required`, `idempotency_key_reused`, `payslip_not_found`, `payout_not_found`, `payout_not_retryable`, `payout_not_blocked`, `retry_limit_reached`, `employee_bank_details_missing`, `attempt_not_current`. All are defined in PAY-602 or the requirement that raises them (R3), `resolve_too_early`.
- **Request models** use `model_config = ConfigDict(extra="forbid")`. Response models use `ConfigDict(from_attributes=True)`.
- **Shared Pydantic types** (`api/schemas.py`):

```python
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Email = Annotated[EmailStr, StringConstraints(max_length=320)]
SalaryCents = Annotated[int, Field(ge=1, le=1_000_000_000)]
RateCents = Annotated[int, Field(ge=1, le=100_000)]
Routing = Annotated[str, StringConstraints(pattern=r"^[0-9]{9}$")]       # plus the ABA checksum validator
Account = Annotated[str, StringConstraints(pattern=r"^[0-9]{4,17}$")]
HoursHundredths = Annotated[int, Field(ge=0, le=74_400)]                # schedule bound in the domain
Limit = Annotated[int, Field(ge=1, le=100)]
IdemKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,200}$")]
Schedule = Literal["biweekly", "monthly"]
PayType = Literal["salaried", "hourly"]

class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None
```

### 4.2 Company settings (PAY-200)

**`GET /v1/company/settings`** (any role) returns 200 `CompanySettingsOut`. Errors: 404 `company_settings_not_found`.

**`PUT /v1/company/settings`** (admin) takes body `CompanySettingsIn` (full replace) and returns 200 `CompanySettingsOut`. Errors: 404 `company_settings_not_found`, 409 `anchor_locked`, 422 `validation_error`.

```python
class CompanySettingsIn(BaseModel):
    name: Name
    tax_rate_bps: Annotated[int, Field(ge=0, le=5000)]
    retirement_bps: Annotated[int, Field(ge=0, le=2000)]
    timezone: Annotated[str, StringConstraints(min_length=1, max_length=64)]   # zoneinfo-valid
    biweekly_anchor: date                                                       # must be a Monday
    require_dual_approval: bool

class CompanySettingsOut(CompanySettingsIn):
    updated_at: datetime
    updated_by: str | None
```

The transaction:
1. Locks `company_settings` with `FOR UPDATE` (404 if missing).
2. **Anchor lock (R33, PAY-200):** if `biweekly_anchor` differs from the stored value and `EXISTS (SELECT 1 FROM pay_runs WHERE company_id=:cid AND schedule='biweekly' AND deleted_at IS NULL)`, it raises `AnchorLocked` → 409 `anchor_locked`, and nothing changes. The check runs after the row lock, and run creation reads the settings row `FOR SHARE` (§4.4), so a biweekly run can't be created with the old anchor while the anchor changes.
3. Diffs the fields, updates the row (`updated_by = sub`) and writes `settings.updated` with `{changed: {field: {old, new}}}`. If nothing changed, it writes no audit row.

The `timezone` field has no consumer in 0.1.0: every period input is a date.

### 4.3 Employees (PAY-201–204)

| Method, path | Role | Request | Success | Errors |
|---|---|---|---|---|
| `GET /v1/employees?status=&cursor=&limit=` | any | query: `status: Literal["active","inactive"] \| None`, `cursor: str \| None`, `limit: Limit = 50` | 200 `Page[EmployeeOut]`, ordered by `name, id` | 422 |
| `POST /v1/employees` | payroll_admin | `EmployeeCreate` | 201 `EmployeeOut` | 409 `employee_email_taken`, 422 |
| `GET /v1/employees/{employee_id}` | any | — | 200 `EmployeeOut` | 404 `employee_not_found` |
| `PATCH /v1/employees/{employee_id}` | payroll_admin | `EmployeeUpdate` | 200 `EmployeeOut` | 404 `employee_not_found`, 409 `employee_email_taken`, 422 |

```python
class EmployeeCreate(BaseModel):
    name: Name
    email: Email
    pay_type: PayType
    annual_salary_cents: SalaryCents | None = None
    hourly_rate_cents: RateCents | None = None
    schedule: Schedule
    bank_routing: Routing
    bank_account: Account
    # model_validator: salaried ⇒ annual_salary_cents set and hourly_rate_cents None; hourly ⇒ the reverse

class EmployeeUpdate(BaseModel):                       # at least one field (model_validator)
    name: Name | None = None
    email: Email | None = None
    pay_type: PayType | None = None
    annual_salary_cents: SalaryCents | None = None
    hourly_rate_cents: RateCents | None = None
    schedule: Schedule | None = None
    bank_routing: Routing | None = None
    bank_account: Account | None = None
    status: Literal["active", "inactive"] | None = None

class EmployeeOut(BaseModel):
    id: UUID
    name: str
    email: str
    pay_type: PayType
    annual_salary_cents: int | None
    hourly_rate_cents: int | None
    schedule: Schedule
    account_last4: str | None          # never the full numbers (PAY-202, PAY-NFR-08)
    has_bank_details: bool
    bank_updated_at: datetime | None
    bank_updated_by: str | None
    status: Literal["active", "inactive"]
    deactivated_at: datetime | None
    created_at: datetime
    updated_at: datetime
```

Rules:
- **PATCH validation:** PATCH merges the update onto the current row, then validates the merged pay basis. Changing `pay_type` without the matching amount gives 422, and the other amount field is set to NULL.
- **Reactivation** (`status: active`, PAY-203) of an employee whose bank details were purged needs both `bank_routing` and `bank_account` in the same PATCH, or the response is 422 `validation_error` with `details = [{field: "body.bank_account", issue: "required_on_reactivation"}]` **[decision]**. It clears `deactivated_at` (and `bank_purged_at` when new details are given).
- **Deactivation** sets `deactivated_at = now()`.
- **Bank changes:** a PATCH that sets `bank_routing` or `bank_account` re-encrypts that field with the active kid, updates `account_last4` (when the account changes), and sets `bank_updated_by = sub` and `bank_updated_at = now()`.
- **Concurrency:** the PATCH transaction does `SELECT … FROM employees WHERE id=:id AND company_id=:cid FOR UPDATE`. It never locks `pay_runs` (§4.10).
- **Email uniqueness:** a violation of `uq_employees_company_email` is mapped to 409 `employee_email_taken`.

### 4.4 Pay runs (PAY-300–304, PAY-400–401, PAY-500–502)

| Method, path | Role | Request | Success | Errors |
|---|---|---|---|---|
| `GET /v1/pay-runs?status=&cursor=&limit=` | any | query: `status: RunStatus \| None`, `cursor`, `limit: Limit = 50` | 200 `Page[PayRunOut]`, ordered by `period_start DESC, id DESC`, excluding deleted runs | 422 |
| `POST /v1/pay-runs` | payroll_admin | `PayRunCreate` | 201 `PayRunOut` | 409 `pay_run_exists`, 409 `no_employees`, 409 `employee_in_overlapping_run`, 422 `invalid_period_start`, 422 |
| `GET /v1/pay-runs/{run_id}` | any | — | 200 `PayRunOut` | 404 `pay_run_not_found` |
| `DELETE /v1/pay-runs/{run_id}` | payroll_admin | — | 204 | 404 `pay_run_not_found`, 409 `pay_run_locked` |
| `PUT /v1/pay-runs/{run_id}/hours` | payroll_admin | `HoursIn` | 200 `HoursOut` | 404 `pay_run_not_found`, 409 `pay_run_locked`, 422 `not_hourly`, 422 `employee_not_in_run`, 422 `validation_error` (bounds) |
| `POST /v1/pay-runs/{run_id}/calculate` | payroll_admin | no body | 200 `PayRunOut` | 404 `pay_run_not_found`, 409 `pay_run_locked`, 422 `hours_missing` |
| `POST /v1/pay-runs/{run_id}/approve` | approver | header `Idempotency-Key: IdemKey` (required), no body | 200 `PayRunOut` | 400 `idempotency_key_required`, 404 `pay_run_not_found`, 409 `pay_run_not_approvable`, 403 `approver_must_differ`, 409 `employee_bank_details_missing`, 422 `idempotency_key_reused`, 422 `validation_error` (malformed key) |
| `GET /v1/pay-runs/{run_id}/payslips?cursor=&limit=` | any | query: `cursor: str \| None`, `limit: Limit = 50` | 200 `Page[PayslipOut]`, ordered by `employee_name, id` (C8) | 404 `pay_run_not_found`, 422 |
| `GET /v1/pay-runs/{run_id}/payslips/{payslip_id}` | any | — | 200 `PayslipOut` | 404 `pay_run_not_found`, 404 `payslip_not_found` |
| `GET /v1/pay-runs/{run_id}/payouts?status=&cursor=&limit=` | any | query: `status: PayoutStatus \| None`, `cursor: str \| None`, `limit: Limit = 50` | 200 `Page[PayoutOut]`, ordered by the payslip's `employee_name`, then `id` (C8) | 404 `pay_run_not_found`, 422 |
| `POST /v1/pay-runs/{run_id}/payouts/{payout_id}/retry` | approver | optional header `Idempotency-Key` **[decision]**, no body | 200 `PayoutOut` | 404 `pay_run_not_found`, 404 `payout_not_found`, 409 `payout_not_retryable`, 409 `retry_limit_reached`, 409 `employee_bank_details_missing`, 403 `approver_must_differ`, 422 `idempotency_key_reused`, 422 `validation_error` (malformed key) |
| `POST /v1/pay-runs/{run_id}/payouts/{payout_id}/resolve` | approver | `ResolveIn`; no `Idempotency-Key` (a repeat finds the attempt no longer blocked) | 200 `PayoutOut` | 404 `pay_run_not_found`, 404 `payout_not_found`, 409 `payout_not_blocked`, 409 `resolve_too_early` (with `Retry-After`; mark_rejected within `PAYOUT_MARK_REJECTED_MIN_WAIT_S`, C48), 422 `validation_error` (unknown action, missing or too long note) |

```python
class PayRunCreate(BaseModel):
    schedule: Schedule
    period_start: date

class RunTotalsOut(BaseModel):
    gross_cents: int
    tax_cents: int
    retirement_cents: int
    net_cents: int
    funding_required_cents: int          # == net_cents (PAY-303)

class PayoutCounts(BaseModel):           # exactly these six keys (C9); all zero before approval
    requested: int                       # payouts in status requested (includes the two below)
    awaiting_bank: int                   # requested payouts whose current attempt is indeterminate
    blocked: int                         # requested payouts whose current attempt is needs_operator
    paid: int                            # includes no_transfer
    no_transfer: int
    failed: int

class PayRunOut(BaseModel):
    id: UUID
    schedule: Schedule
    period_start: date
    period_end: date
    pay_date: date
    period_index: int
    periods_per_year: int
    status: Literal["draft", "calculated", "processing", "paid", "partially_failed"]
    employee_count: int
    tax_bps: int | None
    retirement_bps: int | None
    require_dual_approval: bool | None
    totals: RunTotalsOut | None          # None while draft
    counts: PayoutCounts                 # C9, PAY-303, PAY-413
    created_by: str
    created_at: datetime
    calculated_by: str | None
    calculated_by_name: str | None       # from the token's name claim (R9, C9)
    calculated_at: datetime | None
    approved_by: str | None
    approved_by_name: str | None         # from the token's name claim (R9, C9)
    approved_at: datetime | None
    processing_since: datetime | None
    updated_at: datetime

class HoursEntry(BaseModel):
    employee_id: UUID
    hours_hundredths: HoursHundredths

class HoursIn(BaseModel):
    entries: Annotated[list[HoursEntry], Field(min_length=1, max_length=500)]
    # model_validator: employee_id values unique, otherwise 422 validation_error

class HoursOut(BaseModel):               # PUT …/hours response; the BFF returns it as is and the UI refetches the run (C7)
    pay_run_id: UUID
    status: Literal["draft"]
    entries: list[HoursEntry]            # every hours row now stored for the run, ordered by employee_id

class PayslipOut(BaseModel):
    id: UUID
    pay_run_id: UUID
    employee_id: UUID
    employee_name: str
    pay_type: PayType
    annual_salary_cents: int | None
    hourly_rate_cents: int | None
    hours_hundredths: int | None
    status: Literal["pending", "calculated"]
    gross_cents: int | None
    tax_cents: int | None
    retirement_cents: int | None
    net_cents: int | None

class AttemptOut(BaseModel):
    attempt: int
    status: Literal["requested", "indeterminate", "needs_operator", "settled", "rejected"]
    failure_reason: str | None
    bank_reference: str | None
    account_last4: str
    requested_at: datetime
    final_at: datetime | None

class PayoutOut(BaseModel):               # the web Payout schema mirrors it field for field (C5)
    id: UUID
    pay_run_id: UUID
    payslip_id: UUID                     # R21
    employee_id: UUID
    employee_name: str                   # from the payslip snapshot
    amount_cents: int                    # R21
    currency: Literal["USD"]
    status: Literal["requested", "paid", "failed"]
    no_transfer: bool
    current_attempt: int
    attempts: list[AttemptOut]           # ascending by attempt; [] when no_transfer (PAY-501)

class ResolveIn(BaseModel):              # PAY-502, R8
    action: Literal["redrive", "mark_rejected"]
    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
```

`PayslipOut` amount fields are NULL while the payslip is `pending`, and it has no `schedule` field (C5).
`EmployeeOut` and `AuditEventOut` are likewise mirrored field for field by the web schemas (C5).

#### Behaviour

- **Create (PAY-300).** Lock order is in §4.10.
  1. Load the settings with `SELECT … FROM company_settings WHERE company_id=:cid FOR SHARE` (so the anchor can't change underneath; R33).
  2. Call `derive_period` (`InvalidPeriodStart` → 422).
  3. Check for a non-deleted run on `(cid, schedule, period_start)` → 409 `pay_run_exists`. A unique-index violation in a race maps to the same code.
  4. Load the active employees on `schedule`. If there are none → 409 `no_employees`.
  5. Load coverage for those employees: `SELECT ps.employee_id, r.period_start, r.period_end FROM payslips ps JOIN pay_runs r ON r.id = ps.pay_run_id WHERE r.company_id=:cid AND r.deleted_at IS NULL AND ps.employee_id = ANY(:ids) AND r.period_start <= :new_end AND r.period_end >= :new_start`. Then call `find_overlapping_employees`. Any hit → 409 `employee_in_overlapping_run` with `employee_ids`.
  6. Insert the run (`draft`, with `employee_count`) and one `pending` payslip per employee (snapshotting `employee_name`, `pay_type`, salary or rate).
  7. Write the `payrun.created` audit row.
  - **Documented limitation (R34):** the overlap rule may block a whole period for a schedule when one employee who moved schedule is already covered by another run. That period can be created only after the moved employee is deactivated or moved back. Changing a run's employee set is out of scope for 0.1.0 (R37).
- **Hours (PAY-301).**
  1. `assert_run_mutable`.
  2. Every `employee_id` must have a payslip in the run, else 422 `employee_not_in_run`. Its payslip must be `hourly`, else 422 `not_hourly`. `hours_hundredths ≤ max_hours_hundredths(run.schedule)`, else 422 `validation_error`.
  3. Upsert `pay_run_hours` (`ON CONFLICT (pay_run_id, employee_id) DO UPDATE`).
  4. If the run was `calculated`, revert it: status `draft`; clear `calculated_by`, `calculated_by_name`, `calculated_at`, `tax_bps`, `retirement_bps`, `require_dual_approval` and the totals; set every payslip back to `pending` with its amounts and `hours_hundredths` NULL.
  5. Write the `payrun.hours_updated` audit row.
- **Calculate (PAY-302).**
  1. `assert_run_mutable`.
  2. Read the settings (`tax_rate_bps`, `retirement_bps`, `require_dual_approval`).
  3. Build a `PayslipInput` per payslip, joining `pay_run_hours`.
  4. Call `calculate_run` (`HoursMissing` → 422).
  5. Update each payslip (`calculated`, amounts, `hours_hundredths`).
  6. Set the run's `tax_bps`, `retirement_bps` and `require_dual_approval` snapshots, the totals from `run_totals(stored amounts)`, `calculated_by = sub`, `calculated_by_name = principal.name`, `calculated_at = now()` and status `calculated`.
  7. Write the `payrun.calculated` audit row. Increment `payroll_pay_runs_total{outcome="calculated"}`.
  - Calculating a run that is already `calculated` recomputes it (allowed).
- **Delete (PAY-304).** `assert_run_mutable`, then set `deleted_at = now()` and `deleted_by = sub`, and write `payrun.deleted`. Payslips and hours stay. Later GETs return 404 `pay_run_not_found`.
- **Approve (PAY-400, PAY-401).** Wrapped by the idempotency handling (§4.8).
  1. Lock the run (`FOR UPDATE`). `assert_run_approvable` → 409 `pay_run_not_approvable`.
  2. Read the run's employees with `SELECT … FROM employees WHERE id = ANY(:ids) AND company_id=:cid ORDER BY id FOR SHARE`.
  3. Call `check_approve_dual` with the run's `require_dual_approval` snapshot → 403.
  4. Every employee with a payslip net > 0 must have bank details, else 409 `employee_bank_details_missing` with `employee_ids` (PAY-400, C40, R34). This covers employees purged (§5.4.1) or reactivated without details.
  5. For each payslip, ordered by id:
     - **net = 0:** insert a payout with `paid`, `no_transfer = true`, `current_attempt = 0` and `final_at = now()`.
     - **net > 0:** insert a payout with `requested` and `current_attempt = 1`. Then:
       - decrypt the employee's routing and account;
       - insert `payout_attempts(attempt=1, status=requested, bank_snapshot_enc=encrypt(snapshot, snapshot_aad), account_last4, requested_by=sub, requested_at=now, last_published_at=now, publish_count=1)`;
       - call `OutboxWriter.add_payout_requested(..., republish_seq=0)`.
     - Use bulk `INSERT … VALUES` in chunks of 500.
  6. Compute `status = run_status_from_payouts(...)`. Set `approved_by = sub`, `approved_by_name = principal.name` and `approved_at = now()`. If the status is `processing`, set `processing_since = now()`. If it is `paid`, set `first_final_at = now()` and observe `payroll_payout_settlement_seconds` = 0.
     - The UPDATE is conditional: `UPDATE pay_runs SET … WHERE id=:id AND status='calculated'`. A rowcount of 0 raises `PayRunNotApprovable`.
  7. Write the `payrun.approved` audit row. Increment `payroll_pay_runs_total{outcome="approved"}`, `payroll_payouts_total{outcome="no_transfer"}` by the no-transfer count, and `payroll_pay_runs_total{outcome="paid"}` if the run went straight to `paid`.
  8. Commit, then wake the relay.
- **Retry (PAY-500).** `run_id` and `payout_id` must match (`payout.pay_run_id = run_id`), else 404 `payout_not_found`.
  1. Lock the run, then the payout (`FOR UPDATE`), then the current attempt (`FOR UPDATE`), then the employee (`FOR SHARE`).
  2. Run `check_retry`, then `check_retry_dual`.
  3. Decrypt the employee's current bank details. Insert attempt `n+1` (`requested`, new snapshot, `publish_count = 1`, `requested_at = last_published_at = now`).
  4. Set the payout to `status = requested`, `current_attempt = n+1` and `final_at = NULL`.
  5. Write the outbox row (`republish_seq = 0`).
  6. Recompute the run status. If it moved into `processing`, set `processing_since = now()`.
  7. Write the `payout.retry_requested` audit row.
  8. Log `payout.retry_requested`, then commit and wake the relay.
- **Resolve (PAY-502, R8).** `run_id` and `payout_id` must match, else 404 `payout_not_found`.
  1. Lock the run (`FOR UPDATE`), then the payout (`FOR UPDATE`), then the current attempt (`FOR UPDATE`), the same order as retry and result (§4.10).
  2. `check_resolve(payout_status, attempt_status)` → 409 `payout_not_blocked` unless the current attempt is `needs_operator`.
  3. Apply `resolve_transition(action)` (§3.5a) with conditional updates:
     - **redrive:** `UPDATE payout_attempts SET status='requested', failure_reason=NULL, publish_count=1, last_published_at=now(), needs_operator_at=NULL WHERE id=:aid AND status='needs_operator'`, then `OutboxWriter.add_payout_requested(..., amount_cents = payouts.amount_cents, republish_seq=0)` (fresh `event_id`; C13).
     - **mark_rejected:** `UPDATE payout_attempts SET status='rejected', failure_reason='operator_rejected', final_at=now(), needs_operator_at=NULL WHERE id=:aid AND status='needs_operator' AND now() - needs_operator_at >= make_interval(secs => :min_wait_s)` (rowcount 0 with the attempt still `needs_operator` → `ResolveTooEarly`), then `UPDATE payouts SET status='failed', final_at=now() WHERE id=:pid AND status='requested'`. Increment `payroll_payouts_total{outcome="operator_rejected"}`.
     - A rowcount of 0 raises `ConcurrentUpdate` (500).
  4. Recompute the run status as the result endpoint does (§4.7 step 5): a move into a final status sets `first_final_at` if NULL and observes `payroll_payout_settlement_seconds` once, increments `payroll_pay_runs_total{outcome}`, and logs `payrun.completed`.
  5. Write the `payout.operator_resolved` audit row `{attempt, action, note_len}`. The note's text is never stored, logged or traced (PAY-NFR-07); only its length is.
  6. Log `payout.operator_resolved`, commit, and for a redrive wake the relay.
  - The runbook `PayoutNeedsOperator.md` requires checking the bank first (`GET /v1/transfers/{key}` shows no settled transfer) before mark_rejected (R8). Payroll also enforces the C48 wait: `needs_operator_at` is set on entry to `needs_operator` and cleared by redrive.
  - The wait must exceed the worker's bound on in-flight POSTs: the worker never starts a bank POST later than `HANDLER_DEADLINE_S` (35 s) after its instruction fetch. A startup check requires `PAYOUT_MARK_REJECTED_MIN_WAIT_S > HANDLER_DEADLINE_S + 30`.
- **Read endpoints** run plain read-committed SELECTs with no locks. `PayRunOut.counts` comes from one query: `SELECT p.status, a.status AS attempt_status, p.no_transfer, count(*) FROM payouts p LEFT JOIN payout_attempts a ON a.payout_id = p.id AND a.attempt = p.current_attempt WHERE p.pay_run_id=:id GROUP BY 1,2,3`, folded into the six keys: `requested` = all `requested` payouts, `awaiting_bank` = those whose attempt is `indeterminate`, `blocked` = those whose attempt is `needs_operator`, `paid` = all `paid` (including `no_transfer`), `no_transfer`, `failed`.
- **Payslip and payout lists** are keyset-paginated (C8, §4.9). The payouts list joins `payslips` for `employee_name` and loads the attempts of the page's payouts in one `WHERE payout_id = ANY(:ids) ORDER BY payout_id, attempt` query.

### 4.5 Audit events (PAY-200, PAY-203, PAY-300, PAY-302, PAY-304, PAY-400, PAY-405, PAY-500, PAY-502)

**`GET /v1/audit-events?entity_type=&entity_id=&event=&cursor=&limit=`** (admin) returns 200 `Page[AuditEventOut]`, newest first (`id DESC`; UUIDv7 is time-ordered).

```python
class AuditEventOut(BaseModel):
    id: UUID
    at: datetime                         # created_at
    actor_id: str
    event: str
    entity_type: Literal["company_settings", "employee", "pay_run", "payout"]
    entity_id: UUID
    details: dict[str, Any]
```

### 4.6 Internal: payout instruction (PAY-411)

**`GET /v1/internal/payouts/{payout_id}/instruction?attempt={n}`**
- **Caller:** `iss=payout-worker`, `sub=service:payout-worker` (§8.2).
- **Query:** `attempt: Annotated[int, Field(ge=1)]` (required).
- **Success:** 200 `InstructionOut`.
- **Errors:**
  - 401 `invalid_token` for any other issuer (PAY-411), and 403 `forbidden_role` for an `iss=payout-worker` token whose `sub` isn't `service:payout-worker` (C30; §8.2);
  - 404 `payout_not_found` when the payout doesn't exist or `payout.company_id != cid`;
  - 409 `attempt_not_current` with `details.current_attempt` unless `instruction_allowed(...)`;
  - 500 `internal_error` on `FieldDecryptError`;
  - 422 `validation_error` for a missing or invalid `attempt`.

```python
class InstructionOut(BaseModel):
    payout_id: UUID
    attempt: int
    amount_cents: Annotated[int, Field(ge=1)]
    currency: Literal["USD"]
    routing: str                         # 9 digits, decrypted from the attempt's snapshot
    account: str                         # 4–17 digits, decrypted from the attempt's snapshot
    requested_at: datetime               # the worker's BANK_KEY_SAFE_WINDOW_S check (PAY-410)
```

- **No locks:** this is a single read-committed `SELECT p.*, a.* FROM payouts p JOIN payout_attempts a ON a.payout_id=p.id AND a.attempt=:n WHERE p.id=:id`.
- **Response headers:** `Cache-Control: no-store`.
- **Logging:** the response body is never logged, since `routing` and `account` are deny-listed keys anyway. A 409 logs `payout.attempt_not_current` (source `instruction`).

### 4.7 Internal: payout result (PAY-405, PAY-406, PAY-409)

**`POST /v1/internal/payouts/{payout_id}/result`**
- **Caller:** the worker only.
- **Header:** `Idempotency-Key: <event_id>:<outcome>` (required; §4.8).
- **Body:** `ResultIn`.
- **Success:** 200 `ResultOut`, both when the result is applied and when the attempt is already final.
- **Errors:**
  - 400 `idempotency_key_required` (C34);
  - 401 `invalid_token` (any issuer other than `payout-worker`); 403 `forbidden_role` (wrong `sub`; C30);
  - 404 `payout_not_found` (unknown, or another `cid`);
  - 409 `attempt_not_current` with `details.current_attempt`;
  - 422 `validation_error` (body, or a malformed `Idempotency-Key`);
  - 422 `idempotency_key_reused`.

```python
class ResultIn(BaseModel):
    attempt: Annotated[int, Field(ge=1)]
    outcome: Literal["settled", "rejected", "indeterminate", "poison"]
    reason: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,100}$")] | None = None
    bank_reference: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None   # C26
    # model_validator:
    #   settled  ⇒ bank_reference required, reason None
    #   rejected ⇒ reason required (e.g. account_closed, insufficient_funds_at_bank)
    #   indeterminate ⇒ both None
    #   poison   ⇒ reason optional (amount_mismatch | schema_invalid | key_window_expired)

class ResultOut(BaseModel):              # exactly {payout_id, attempt, outcome} (R7, PAY-405)
    payout_id: UUID
    attempt: int
    outcome: Literal["requested", "indeterminate", "needs_operator", "settled", "rejected"]
    # the stored attempt status after the call, whatever was posted (C26). The worker treats
    # settled and rejected as final and every other value as recorded.
```

Whether the call changed anything is not in the response; it is in the log `payout.result_recorded` (`applied`) and the audit row.

**Transaction.**
1. Read the payout without a lock to find `pay_run_id`. If it's missing or the `cid` differs → 404.
2. Lock the run (`FOR UPDATE`).
3. Lock the payout (`FOR UPDATE`).
4. Lock the attempt row `(payout_id, attempt)` (`FOR UPDATE`).
5. Call `decide_result`.
   - **NOT_CURRENT:** roll back and return 409. Log `payout.attempt_not_current` (source `result`).
   - **STORED:** return 200 with `outcome` = the stored attempt status, and no change. Log `payout.result_recorded` with `applied=false`. Store the idempotency row.
   - **APPLY:** first run `UPDATE payout_attempts SET status=:new, failure_reason=:reason, bank_reference=:ref, final_at=CASE WHEN :new IN ('settled','rejected') THEN now() END, needs_operator_at=CASE WHEN :new='needs_operator' THEN now() WHEN :new IN ('settled','rejected') THEN NULL ELSE needs_operator_at END WHERE id=:aid AND status=:expected_status` (belt and braces; a rowcount of 0 raises `ConcurrentUpdate`, which becomes 500). Then:
     - set the payout status to `payout_status_for_attempt(new)`, and set `final_at` when it is final;
     - recompute the run status with `run_status_from_payouts(SELECT status FROM payouts WHERE pay_run_id=:rid)`, using `UPDATE pay_runs SET status=:s … WHERE id=:rid AND status IN ('processing','paid','partially_failed')`. If the status enters `processing`, set `processing_since`. If it enters a final status and `first_final_at IS NULL`, set `first_final_at = now()` and observe `payroll_payout_settlement_seconds = first_final_at − approved_at`;
     - write the `payout.result_recorded` audit row (plus `payout.needs_operator` for poison);
     - update the metrics and logs (§7);
     - return 200 with `outcome` = the new attempt status.
6. Store the idempotency row and commit.

### 4.8 Idempotency-Key handling

| Route | Key | Header absent |
|---|---|---|
| `POST /v1/pay-runs/{id}/approve` | Required. Supplied by the BFF from the browser | 400 `idempotency_key_required` |
| `POST /v1/internal/payouts/{id}/result` | Required. `<event_id>:<outcome>` | 400 `idempotency_key_required` |
| `POST /v1/pay-runs/{id}/payouts/{pid}/retry` | Optional; honoured when present **[decision]** | processed normally |

- **Missing vs malformed (C34):** an absent header on a required route is 400 `idempotency_key_required`. A header present but not matching `IdemKey` (`^[A-Za-z0-9._:-]{1,200}$`) is 422 `validation_error` with `details = [{field: "header.Idempotency-Key", issue: "string_pattern_mismatch"}]`, on every route that reads the header.
- **Other mutations** (create, hours, calculate, delete, resolve, employee and settings writes) take no key; the BFF never retries them.
- **Table:** `idempotency_keys` (§2.10), owned by payroll's DDL (C22).
- **`scope`:** `f"{iss}|{sub}|{method} {route_template}|{sorted path params as k=v joined by ','}"`. So the same key on another run, or from another user, never collides.
- **`request_hash`:** `sha256(method + "\n" + route_template + "\n" + canonical_path_params + "\n" + canonical_body).hexdigest()`. `canonical_body` is `json.dumps(parsed_body, sort_keys=True, separators=(",", ":"))`, or `""` when there is no body.
- **TTL:** `expires_at = now() + IDEMPOTENCY_TTL_H` (24 h). Maintenance purges rows past `expires_at` (§5.4).
- **Algorithm** (`api.deps.idempotency`, the first statements of the handler's transaction, before any domain lock):
  1. Run `DELETE FROM idempotency_keys WHERE company_id=:cid AND scope=:scope AND key=:key AND expires_at < now()`.
  2. Run `INSERT INTO idempotency_keys (company_id, scope, key, request_hash, response_status, response_body, expires_at) VALUES (…, 0, 'null', …) ON CONFLICT DO NOTHING RETURNING key`.
     - A concurrent request with the same key blocks here on the unique index until the first transaction ends. So two in-flight duplicates are serialised, and the second sees the committed result.
  3. If no row is returned, `SELECT request_hash, response_status, response_body`:
     - a different hash → 422 `idempotency_key_reused`, and log `idempotency.key_reused`;
     - the same hash → replay `response_status` and `response_body` with the header `Idempotent-Replayed: true`, and log `idempotency.replayed`. Nothing else runs.
  4. Otherwise run the handler. On a 2xx, `UPDATE idempotency_keys SET response_status, response_body` in the **same transaction**, then commit.
  5. On any error the transaction rolls back, so the key is not stored, and a retry with the same key re-evaluates. Errors are never replayed; 4xx domain errors are deterministic for the same state.
- **Consequence:** a repeat approve with the same key and body returns the original 200 body, the run as it was at approval (PAY-400). A repeat with a new key returns 409 `pay_run_not_approvable`.

### 4.9 Pagination

- **Cursor:** `base64url_nopad(json.dumps({"v": 1, "after": "<uuid>"}))`, where `after` is the ID of the last item on the previous page. It holds only an ID, so no names, emails or periods appear in URLs **[decision]**.
- **Keyset query:** the server resolves the sort key of `after` with a subquery in the same company and parent scope, then filters `(sort_cols) > (subquery)` (or `<` for descending orders). It fetches `limit + 1` rows; `next_cursor` is set when the extra row exists, and `null` otherwise.
- **Orders:**

  | List | Order |
  |---|---|
  | employees | `name ASC, id ASC` |
  | pay-runs | `period_start DESC, id DESC` |
  | payslips, payouts | `employee_name ASC, id ASC` |
  | audit-events | `id DESC` |

- **Response shape (R4, C8):** every list, including payslips and payouts, is `Page[T]` = `{items, next_cursor}`, where `next_cursor` is an opaque base64url string or `null`.
- **Errors:** an undecodable cursor, a wrong `v`, or an `after` ID that isn't found in scope → 422 `validation_error` with `details = [{field: "query.cursor", issue: "invalid_cursor"}]` (C14).
- **Limit:** 1–100, default 50 (high-level design §7).

### 4.10 Transaction and lock order

Every mutating use case runs in **one** transaction (`session.begin()`) at READ COMMITTED. The global
lock order is below. A use case takes only a subset, but always in this order, so no deadlock cycle
can form:

1. `idempotency_keys` row (§4.8)
2. Advisory transaction lock `pg_advisory_xact_lock(hashtext('payroll.create_run:' || company_id))` (create only)
3. `pay_runs` row `FOR UPDATE`
4. `payouts` rows `FOR UPDATE` (ordered by `id`)
5. `payout_attempts` rows `FOR UPDATE` (ordered by `payout_id, attempt`)
6. `employees` rows `FOR SHARE` (run mutations) or `FOR UPDATE` (employee PATCH and purge), ordered by `id`
7. `company_settings` row (plain read; `FOR SHARE` in create run; `FOR UPDATE` only in PUT settings)
8. Inserts: `payslips`, `pay_run_hours`, `payouts`, `payout_attempts`, `outbox`, `audit_events`

| Use case | Locks taken (in order) | Conditional update |
|---|---|---|
| Create run | 2 → 7 (`FOR SHARE`) → employees (no lock) → insert run and payslips | Unique index `uq_pay_runs_company_schedule_period` |
| Hours | 3 → upsert hours, update payslips | `assert_run_mutable` after the lock |
| Calculate | 3 → payslips read → read settings | `UPDATE pay_runs … WHERE status IN ('draft','calculated')` |
| Delete | 3 | `… WHERE status IN ('draft','calculated') AND deleted_at IS NULL` |
| Approve | 1 → 3 → 6 (FOR SHARE) → inserts | `… WHERE status='calculated'` |
| Retry | (1) → 3 → 4 → 5 → 6 (FOR SHARE) → inserts | Attempt `… WHERE status='rejected'`; payout `… WHERE status='failed'` |
| Resolve | 3 → 4 → 5 → inserts (outbox for redrive) | Attempt `… WHERE status='needs_operator'`; payout `… WHERE status='requested'` |
| Result | 1 → 3 → 4 → 5 | Attempt `… WHERE status=:expected` |
| Re-publish (maintenance task) | 3 (every affected run, ordered by `id`) → 5 → outbox insert | Attempt `… WHERE status IN ('requested','indeterminate') AND publish_count=:seen` |
| PUT settings | 7 (`FOR UPDATE`) → `pay_runs` read (no lock) | `anchor_locked` check after the lock |
| Employee PATCH | 6 (FOR UPDATE) | — |
| Bank purge (per employee) | 5 (the employee's attempt rows) → 6 (FOR UPDATE) | Re-checks "no non-final attempt" after the locks |
| Outbox relay | outbox rows `FOR UPDATE SKIP LOCKED` → `payout_attempts.last_published_at` update | — |

- **Why the relay is safe:** it holds only outbox row locks when it touches `payout_attempts`. No other transaction waits on an existing outbox row's lock, so this cannot form a cycle.
- **Why approve cannot deadlock with PATCH:** PATCH never locks `pay_runs`.
- **Why the re-publish task cannot deadlock:** it is the only transaction that locks several `pay_runs` rows, and it takes them in `id` order before any payout or attempt row; every other transaction locks at most one run, also first.
- **Why create cannot deadlock with PUT settings:** create holds the advisory lock (2) and then `FOR SHARE` on settings (7); PUT settings takes only the settings row.
- **Timeouts:** `DB_STATEMENT_TIMEOUT_MS` (2000) applies to every statement. A `QueryCanceled` maps to 503 `db_timeout`.

### 4.11 Health

`/livez` and `/readyz` come from pycommon (PAY-605). `/readyz` checks:
- `SELECT 1`;
- Redis `PING` as the payroll ACL user;
- the Alembic head.

They need no JWT, are excluded from tracing, and are never exposed through ingress.

---

## 5. Events

### 5.1 Outbox row and envelope (PAY-402)

`OutboxWriter.add_payout_requested(session, *, company_id, payout_id, pay_run_id, attempt, amount_cents, republish_seq)`:
- `event_id = uuid7()`, which is also `outbox.id`.
- `traceparent`: if the current span context is valid, it is the W3C injection of that context (`TraceContextTextMapPropagator().inject(carrier)`). Otherwise it is a fresh, valid, non-sampled context (`00-<random 32 hex>-<random 16 hex>-00`), so the schema's `traceparent` is always present (`pycommon.streams.Envelope.traceparent` is required; C22).
- `occurred_at`: UTC time in RFC 3339 with `Z` and millisecond precision.
- **Payload** (validated against `contracts/events/payout.requested.v1.json` by `events.envelope.validate_envelope` when `PAYROLL_VALIDATE_EVENTS` is true; a failure raises, which becomes 500, since it's a bug). The example uses the dev `COMPANY_ID` (C28):

```json
{
  "event_id": "0193f1d2-6c4a-7b1e-9a3f-2c5d8e9f0a1b",
  "type": "payout.requested",
  "version": 1,
  "occurred_at": "2026-11-10T14:03:07.123Z",
  "producer": "payroll",
  "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
  "data": {
    "payout_id": "0193f1d2-6c4a-7000-8000-000000000001",
    "pay_run_id": "0193f1d2-6c4a-7000-8000-000000000002",
    "company_id": "0193a000-0000-7000-8000-000000000001",
    "attempt": 1,
    "amount_cents": 412345,
    "currency": "USD",
    "republish_seq": 0
  }
}
```

- `republish_seq` is 0 for the first publication of an attempt (approve or retry). For the k-th re-publish of that attempt it is k (§5.3).
- `outbox.payout_id` and `outbox.attempt` copy `data.payout_id` and `data.attempt`, for the "no unsent outbox row" check.

### 5.2 Relay (`events/relay.py`, PAY-402)

- **Where it runs:** pycommon's relay loop runs as one background task per replica (registered through `create_app(background=…)`), with payroll's `PayrollOutboxSource` as its repository. On shutdown it finishes the current batch, then exits. The outbox table is payroll's own DDL (§2.9); pycommon defines no outbox table (C22).
- **Redis connection:** `pycommon.redis_client.create_redis(settings)` as ACL user `payroll` (`REDIS_USERNAME=payroll`, `REDIS_PASSWORD`; C1), with `socket_timeout = REDIS_SOCKET_TIMEOUT_MS`.
- **Publishing:** `StreamProducer.publish("stream:payout.requested", event_id=str(row.id), type="payout.requested", envelope_json=<json>, maxlen=STREAM_MAXLEN)`, which runs `XADD stream:payout.requested MAXLEN ~ 100000 * event_id <id> type payout.requested envelope <json>` (R7, C22).

`PayrollOutboxSource` (the repository the relay loop calls; every method runs in the loop's batch transaction):

```text
claim_unsent(session, *, limit = PAYROLL_OUTBOX_BATCH (100)) -> Sequence[StreamItem]:
    SELECT id, payload, payout_id, attempt FROM payroll.outbox
    WHERE sent_at IS NULL ORDER BY created_at, id
    LIMIT :limit FOR UPDATE SKIP LOCKED
    -> per row: open the PRODUCER span (below), build envelope_json, and return
       StreamItem(event_id=row.id, type="payout.requested", envelope_json=…, ref=(row.payout_id, row.attempt, span))
mark_sent(session, items):   # ends each item's span (OK)
    UPDATE payroll.outbox SET sent_at = now() WHERE id = ANY(:ids)
    UPDATE payroll.payout_attempts a SET last_published_at = now()
      FROM unnest(:payout_ids, :attempts) AS s(pid, att)
      WHERE a.payout_id = s.pid AND a.attempt = s.att AND a.status IN ('requested','indeterminate')
mark_failed(session, items, *, error_type):   # ends each item's span (ERROR)
    UPDATE payroll.outbox SET relay_attempts = relay_attempts + 1, last_error = :error_type WHERE id = ANY(:ids)
```

Per row, inside `claim_unsent` (the span travels in `StreamItem.ref` and is closed by `mark_sent`/`mark_failed`):

```text
ctx  = extract(row.payload.traceparent)
span = tracer.start_span("payroll.outbox.publish", context=ctx, kind=PRODUCER,
                         attributes={messaging.system: "redis", messaging.destination.name:
                         "stream:payout.requested", messaging.message.id: row.id, payroll.payout_id,
                         payroll.attempt})
envelope_json = json(row.payload with traceparent := inject(span context))   # worker becomes a child of the publish span
```

- **Loop:** wait for the wake event or `PAYROLL_OUTBOX_POLL_MS` (200 ms), whichever is first; this is pycommon `RelayLoop` (platform §3.9, C22) over `PayrollOutboxSource`: it calls `claim_unsent`, publishes the batch in one `publish_many` pipeline (under `REDIS_SOCKET_TIMEOUT_MS`), then `mark_sent` or `mark_failed`, and commits. On any failure log `outbox.relay.error` (`error_type`, `batch_size`, `failed`, `backoff_ms`) and back off `min(0.2 × 2^k, 5)` s with full jitter; a clean batch resets `k`. A full batch loops at once.
- **Stream fields** (the contract with payout-worker, R7):
  - `event_id`: string;
  - `type`: `"payout.requested"`;
  - `envelope`: the JSON string of the full envelope, which the worker validates against the schema.
- **Delivery** is at least once: a crash between XADD and COMMIT re-sends the row, and the worker tolerates duplicates (instruction check, `processed_events`).
- **Why `last_published_at` is updated at send time [decision]:** an outbox row that waited during a Redis outage doesn't count toward the re-publish backoff, so the first re-publish can't land right behind a late send.
- **Scaling:** several replicas run relays safely, because each batch is claimed with SKIP LOCKED (plan §12.1 "Outbox relay crash").
- **Wake-up:** `OutboxWriter` registers an `after_commit` hook that sets the local relay's wake event, so approve publishes within milliseconds.
- **Gauges:** `outbox_unsent` and `outbox_oldest_age_seconds` are payroll's collection-time gauges (§7.2, C18), not values cached by the relay.

### 5.3 Re-publish (`events/republish.py`, PAY-407)

`republish_due_attempts(session, settings, clock) -> int` is the maintenance task `republish` (§5.4). It
runs in the task's single transaction, while the runner holds the maintenance advisory lock:

```text
candidates = SELECT a.id, a.payout_id, a.attempt, p.pay_run_id, p.company_id
  FROM payroll.payout_attempts a JOIN payroll.payouts p ON p.id = a.payout_id
  WHERE a.status IN ('requested','indeterminate')
    AND a.attempt = p.current_attempt
    AND a.last_published_at < now() - make_interval(secs =>
          LEAST(:after_s * power(2, a.publish_count - 1), :max_backoff_s))
    AND NOT EXISTS (SELECT 1 FROM payroll.outbox o
                    WHERE o.payout_id = a.payout_id AND o.attempt = a.attempt AND o.sent_at IS NULL)
  ORDER BY a.last_published_at
  LIMIT :PAYROLL_REPUBLISH_BATCH (1000)

SELECT … FROM pay_runs WHERE id = ANY(:run_ids) ORDER BY id FOR UPDATE
SELECT … FROM payout_attempts WHERE id = ANY(:ids) ORDER BY payout_id, attempt FOR UPDATE
for each attempt:
  action = domain.retry.republish_decision(status, is_current, publish_count, last_published_at, now,
                                           has_unsent_outbox (re-queried), after_s, max_backoff_s, max_republish)
  REPUBLISH:
    OutboxWriter.add_payout_requested(..., amount_cents = payouts.amount_cents, republish_seq = publish_count)
    UPDATE payout_attempts SET publish_count = publish_count + 1, last_published_at = now()
      WHERE id = :aid AND publish_count = :seen AND status IN ('requested','indeterminate')
    payroll_payouts_republished_total += 1; log payout.republished
  NEEDS_OPERATOR:
    UPDATE payout_attempts SET status = 'needs_operator', failure_reason = 'republish_limit', needs_operator_at = now()
      WHERE id = :aid AND status IN ('requested','indeterminate')
    audit payout.needs_operator; payroll_payouts_total{outcome="needs_operator"} += 1
    log payout.needs_operator (level error)
  SKIP: nothing
(run status is unchanged: needs_operator keeps the payout requested, so the run stays processing)
return the number of rows re-published plus moved to needs_operator
```

- `after_s` = `PAYOUT_REPUBLISH_AFTER_S` (360, R1), `max_backoff_s` = `PAYOUT_REPUBLISH_MAX_BACKOFF_S` (900), `max_republish` = `PAYOUT_REPUBLISH_MAX` (10).
- `republish_seq` is `publish_count` before the increment (R34), so the first re-publish carries 1.
- Each re-publish is a **new outbox row with a fresh UUIDv7 `event_id`**, carrying the same `payout_id` and `attempt`, so the bank key is the same. The worker's instruction check is the gate (ADR 0003 d).
- Re-publishing stops for `needs_operator`, because `republish_decision` returns SKIP for it. An operator redrive (PAY-502) makes the attempt `requested` again with `publish_count = 1` and `last_published_at = now()` (C13), so the backoff starts over.
- The per-tick batch limit keeps a tick bounded. Due attempts not reached in this tick are handled in the next one.

### 5.4 Maintenance (`maintenance.py`, PAY-407, PAY-608)

Payroll uses `pycommon.maintenance.MaintenanceRunner`:

```python
MaintenanceRunner(engine, sessions, lock_name="payroll:maintenance", interval_s=MAINTENANCE_INTERVAL_S,
                  jitter=0.1, tasks=[
    MaintenanceTask("republish", republish_due_attempts),            # §5.3
    MaintenanceTask("purge_outbox", purge_sent_outbox),              # DELETE FROM outbox WHERE sent_at < now() - RETENTION_OUTBOX_DAYS days (batches of 1000)
    MaintenanceTask("purge_idempotency_keys", purge_idempotency_keys),  # DELETE FROM idempotency_keys WHERE expires_at < now() (batches of 1000)
    MaintenanceTask("purge_bank_details", purge_bank_details),       # §5.4.1
])
```

- **Lock:** each tick (every `MAINTENANCE_INTERVAL_S`, 60 s ± 10%) runs `pg_try_advisory_lock(advisory_lock_key("payroll:maintenance"))` on a dedicated connection. Another replica holding it → skip the tick (pycommon logs `maintenance.skipped` at debug).
- **Tasks** run in the order above, each in its own transaction (committed on success, rolled back on error). A failing task doesn't stop the others.
- **Logs and metrics** are pycommon's (C44): `maintenance.task.completed` {task, affected, duration_ms} at info, `maintenance.task.failed` {task, error_type} at error, and `maintenance_runs_total{task, outcome}` / `maintenance_duration_seconds{task}`. Payroll adds its domain events (`payout.republished`, `payout.needs_operator`, `employee.bank_details_purged`).
- The purge SQL is payroll's own, because the tables are payroll's DDL (C22).

#### 5.4.1 Bank-detail purge (PAY-608, ADR 0004)

```text
ids = SELECT e.id FROM employees e
      WHERE e.status = 'inactive' AND e.bank_account_enc IS NOT NULL
        AND e.deactivated_at < now() - RETENTION_BANK_DETAILS_DAYS days
        AND NOT EXISTS (SELECT 1 FROM payouts p JOIN payout_attempts a ON a.payout_id = p.id
                        WHERE p.employee_id = e.id
                          AND a.status IN ('requested','indeterminate','needs_operator'))
      ORDER BY e.id LIMIT 100
for each id, in order (inside the task's transaction):
  SELECT a.id FROM payout_attempts a JOIN payouts p ON p.id = a.payout_id
    WHERE p.employee_id = :id ORDER BY a.payout_id, a.attempt FOR UPDATE OF a   # lock order 5 before 6 (§4.10)
  SELECT … FROM employees WHERE id = :id FOR UPDATE; re-check every condition above (skip the employee if any fails)
  UPDATE employees SET bank_routing_enc = NULL, bank_account_enc = NULL, account_last4 = NULL,
                       bank_purged_at = now() WHERE id = :id
  UPDATE payout_attempts a SET bank_snapshot_enc = NULL FROM payouts p
    WHERE a.payout_id = p.id AND p.employee_id = :id AND a.status IN ('settled','rejected')
          AND a.bank_snapshot_enc IS NOT NULL
  audit employee.bank_details_purged {deactivated_at, attempts_nulled}
  log employee.bank_details_purged (employee_id, attempts_nulled)
return the number of employees purged
```

- **Effect on a retry:** once an employee is purged, a retry for them returns 409 `employee_bank_details_missing` (plan J5).
- **Effect on a draft or calculated run** that still contains the employee: approve returns the same 409 (§4.4, R34).

---

## 6. Configuration

`Settings` subclasses `pycommon.settings.BaseServiceSettings` (`extra="ignore"`, no env prefix, case-insensitive) and is loaded with `pycommon.settings.load_settings(Settings)`; a validation failure logs `settings.invalid` (field names only) and exits 2.
- **Shared keys** are pycommon's names, the contract for every Python service (C1): `DB_*`, `REDIS_*`, `ENV`, `APP_VERSION`, `LOG_LEVEL`, `DEBUG`, `DOCS_ENABLED`, `COMPANY_ID`, `INTERNAL_JWT_PUBLIC_KEYS`, limits and retention. There is no `DATABASE_URL` or `REDIS_URL`.
- **Payroll-only keys** carry the `PAYROLL_` prefix as part of their field names (for example `payroll_field_keys` ↔ `PAYROLL_FIELD_KEYS`).
- **Shared payout timing keys** (`PAYOUT_*`, `BANK_TOTAL_DEADLINE_S`) come from `x-payout-timing` in Compose and the `payout-timing` ConfigMap in Kubernetes (R11), shared with payout-worker.
- Secrets are `SecretStr`. Defaults are in the code. Kubernetes overlays and `.env.example` repeat them, each with a comment.

### 6.1 Keys

| Key / env var | Type | Default dev / uat / prod | Requirement |
|---|---|---|---|
| `ENV` | `Literal["dev","uat","prod"]` | dev / uat / prod | PAY-605 |
| `APP_VERSION` | str | `0.1.0-dev` / image tag / image tag | observability |
| `PORT` | int (process env, read by `pycommon.serve`) | 8000 (all). `PAYROLL_PORT` (4111) is only the host port in `compose.test.yaml` | — |
| `DB_HOST` / `DB_PORT` / `DB_NAME` | str / int / str | `postgres` / 5432 / `payrun` (dev); managed endpoint (uat, prod) | PAY-606, C1 |
| `DB_USER` | str | `payroll_app` (service); `payroll_migrator` (migrate container) (C2) | PAY-606 |
| `DB_PASSWORD` | SecretStr | `${PAYROLL_DB_PASSWORD}` (service) or `${PAYROLL_DB_MIGRATOR_PASSWORD}` (migrate), from `.env` / External Secrets / External Secrets | PAY-606, plan §8 |
| `DB_SCHEMA` | str | `payroll` (all) | PAY-606 |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | int | 10 / 5 (all) | PAY-NFR-05 |
| `DB_STATEMENT_TIMEOUT_MS` | int | 2000 (all) | plan §2.2, PAY-605 |
| `DB_POOL_TIMEOUT_S` | float | 2 (all) | plan §2.2 |
| `REDIS_HOST` / `REDIS_PORT` | str / int | `redis` / 6379 (dev); managed (uat, prod) | PAY-607, C1 |
| `REDIS_USERNAME` | str | `payroll` (all) | PAY-607 |
| `REDIS_PASSWORD` | SecretStr | `${REDIS_PAYROLL_PASSWORD}` from `.env` / ES / ES | PAY-607 |
| `REDIS_SOCKET_TIMEOUT_MS` | int | 500 (all) | plan §2.2 |
| `STREAM_MAXLEN` | int | 100000 (all) | PAY-402 |
| `PAYROLL_OUTBOX_BATCH` | int (1–1000) | 100 (all) | PAY-402 |
| `PAYROLL_OUTBOX_POLL_MS` | int | 200 (all) | PAY-402 |
| `PAYROLL_VALIDATE_EVENTS` | bool | true (all) | PAY-402 |
| `COMPANY_ID` | UUID | `0193a000-0000-7000-8000-000000000001` (dev and CI; C28) / per env / per env | PAY-200 (migration seed) |
| `PAYROLL_SEED_COMPANY_NAME` | str (migration only) | `PayRun Demo Company` (all) | PAY-200, R22 |
| `PAYROLL_FIELD_KEYS` | SecretStr (JSON `{kid: base64 32 bytes}`) | `.env` (kid `dev-1`) / ES / ES | PAY-204, ADR 0004 |
| `PAYROLL_FIELD_ACTIVE_KID` | str | `dev-1` / non-dev kid / non-dev kid | PAY-204 |
| `INTERNAL_JWT_PUBLIC_KEYS` | str (JSON `{kid: {iss, pem}}`, web and worker keys) | `${PAYROLL_INTERNAL_JWT_PUBLIC_KEYS}` from `.env` / ConfigMap / ConfigMap | PAY-600 |
| `JWT_LEEWAY_S` | int | 5 (all) | PAY-600 |
| `SERVICE_RATE_LIMIT_USER` | int per min | 600 (raised in `compose.test.yaml`) / 600 / 600 | PAY-603 |
| `SERVICE_RATE_LIMIT_WORKER` | int per min | 6000 / 6000 / 6000 | PAY-603 |
| `MAX_BODY_BYTES` | int | 262144 (all) | PAY-604 |
| `PAYOUT_MAX_ATTEMPTS` | int | 5 (all) | PAY-500 |
| `PAYOUT_REPUBLISH_AFTER_S` | int | 360 (all; shared `payout-timing`); passes the §6.2 check (R1) | PAY-407 |
| `PAYOUT_REPUBLISH_MAX_BACKOFF_S` | int | 900 (all; shared) | PAY-407 |
| `PAYOUT_REPUBLISH_MAX` | int | 10 (all; shared) | PAY-407 |
| `PAYOUT_MARK_REJECTED_MIN_WAIT_S` | int | 120 (all; shared; checked against `HANDLER_DEADLINE_S`) | PAY-502, C48 |
| `PAYROLL_REPUBLISH_BATCH` | int | 1000 (all) | PAY-407 |
| `PAYOUT_MAX_DELIVERIES` | int (startup check only) | 5 (all; shared) | PAY-605 |
| `PAYOUT_CLAIM_MIN_IDLE_MS` | int (startup check only) | 60000 (all; shared) | PAY-605 |
| `BANK_TOTAL_DEADLINE_S` | float (startup check only) | 15 (all; shared) | PAY-605 |
| `MAINTENANCE_INTERVAL_S` | int | 60 (all) | PAY-407, PAY-608 |
| `PAYOUT_STUCK_AFTER_S` | int | 600 (all) | plan §11 |
| `RETENTION_OUTBOX_DAYS` | int | 7 (all) | PAY-608 |
| `RETENTION_BANK_DETAILS_DAYS` | int | 30 (all) | PAY-608 |
| `IDEMPOTENCY_TTL_H` | int | 24 (all) | PAY-400, PAY-608 |
| `PAYROLL_GAUGE_QUERY_TIMEOUT_MS` | int | 1000 (all) | §7.2, R26 |
| `DOCS_ENABLED` | bool | true / false / false | PAY-605 |
| `DEBUG` | bool | false (all) | PAY-605 |
| `LOG_LEVEL` | str (case-insensitive; upper-cased by pycommon; C24) | `INFO` (all) | observability |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | str | `http://otel-collector:4317` / per env / per env | PAY-NFR-09 |
| `OTEL_TRACES_SAMPLER` | str | `parentbased_always_on` (all; no `OTEL_TRACES_SAMPLER_ARG`). Prod sampling is the Collector's `tail_sampling` (R27, C23) | plan §2.2, §11 |
| `OTEL_METRIC_EXPORT_INTERVAL` | int ms | 15000 (all) | §7.2, R26 |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | str (process env, set in the Dockerfile) | `http` (all) | observability §2 |

The JWT audience is the constant `payroll` (the service name), not a setting. `PAYOUT_PAYROLL_CALL_DEADLINE_S` (R2) is a payout-worker key; payroll reads it only for its `check_mark_rejected_wait` startup check (C48).

### 6.2 Startup checks (PAY-605, C29)

Payroll calls `pycommon.startup_checks.run_startup_checks(settings, extra=STARTUP_CHECKS)` from the lifespan, before any connection is opened. pycommon's rules apply to every check: in **uat and prod** each failure logs `startup.check_failed` {setting, reason} at error (the setting's name, never its value) and the process exits with code **2**; in **dev** the same failures are logged at warning and start-up continues.

1. **Secrets** (pycommon `check_secrets`): `DB_PASSWORD`, `REDIS_PASSWORD`, `PAYROLL_FIELD_KEYS` and every other `SecretStr` are present and not dev defaults or `.env.example` placeholders. `INTERNAL_JWT_PUBLIC_KEYS`, `PAYROLL_FIELD_ACTIVE_KID` and `COMPANY_ID` are present.
2. **Field keys** (pycommon `check_field_keys(PAYROLL_FIELD_KEYS, PAYROLL_FIELD_ACTIVE_KID)`): the JSON parses, every key decodes to 32 bytes, and the active kid is one of its kids. **No** kid starts with `dev-` or `test-` (ADR 0004, ADR 0008).
3. **JWT keyset** (pycommon `check_jwt_kids`, plus a payroll extra): `INTERNAL_JWT_PUBLIC_KEYS` parses, every entry's `iss` is in `{"web", "payout-worker"}`, no kid starts with `dev-` or `test-`, and there is at least one kid each for `web` and `payout-worker` (PAY-600).
4. **Debug and docs** (pycommon `check_debug_and_docs`): `DEBUG` and `DOCS_ENABLED` are false.
5. **Timing** (pycommon `check_republish_after`, R11, plan §2.2): `PAYOUT_REPUBLISH_AFTER_S > PAYOUT_MAX_DELIVERIES × PAYOUT_CLAIM_MIN_IDLE_MS/1000 + BANK_TOTAL_DEADLINE_S`. With the defaults: 360 > 5 × 60 + 15 = 315 (R1). Payroll extras: `PAYOUT_REPUBLISH_MAX_BACKOFF_S ≥ PAYOUT_REPUBLISH_AFTER_S` and `PAYOUT_MAX_ATTEMPTS ≥ 1`.
6. **mark_rejected wait** (pycommon `check_mark_rejected_wait`, C48): `PAYOUT_MARK_REJECTED_MIN_WAIT_S > (BANK_TOTAL_DEADLINE_S + 2 × PAYOUT_PAYROLL_CALL_DEADLINE_S + 10) + 30`. With the defaults: 120 > 35 + 30 = 65.

   The claim-idle invariant (`PAYOUT_CLAIM_MIN_IDLE_MS/1000 > BANK_TOTAL_DEADLINE_S + 2 × PAYOUT_PAYROLL_CALL_DEADLINE_S + 10`) belongs to payout-worker's checks, not payroll's (R11).

---

## 7. Logging and metrics

### 7.1 Log events

- **Format:** JSON via `pycommon.logging.log_event`. The standard fields are always present: `ts`, `level`, `service`, `env`, `version`, `logger`, `event`, `msg`, `trace_id`, `span_id`, `request_id`.
- **What never appears** (PAY-NFR-07): amounts, salaries, rates, hours, names (including `calculated_by_name` and `approved_by_name`), emails, bank numbers, `account_last4` and the text of a resolve note (its length only). Only IDs, counts, statuses and codes. Actor IDs are user UUIDs or service names.
- The request middleware logs one `http.request` per request.

| Event | Level | Fields | When | Requirement |
|---|---|---|---|---|
| `settings.updated` | info | `company_id`, `actor_id`, `fields` (names) | PUT settings changed something | PAY-200 |
| `settings.anchor_locked` | info | `company_id`, `actor_id` | PUT settings refused with 409 `anchor_locked` | PAY-200, R33 |
| `employee.created` | info | `employee_id`, `actor_id`, `pay_type`, `schedule` | POST employee | PAY-201 |
| `employee.updated` | info | `employee_id`, `actor_id`, `fields` (names), `bank_changed: bool`, `status_changed: bool` | PATCH | PAY-203 |
| `employee.bank_details_purged` | info | `employee_id`, `attempts_nulled` | maintenance purge | PAY-608 |
| `payrun.created` | info | `pay_run_id`, `schedule`, `period_start`, `employee_count`, `actor_id` | create | PAY-300 |
| `payrun.create_rejected` | info | `code` (`pay_run_exists`, `no_employees`, `employee_in_overlapping_run`, `invalid_period_start`), `schedule`, `period_start`, `employee_count` (overlap) | create refused | PAY-300 |
| `payrun.hours_updated` | info | `pay_run_id`, `entries`, `reverted_to_draft` | hours PUT | PAY-301 |
| `payrun.calculated` | info | `pay_run_id`, `payslips`, `tax_bps`, `retirement_bps`, `require_dual_approval`, `duration_ms`, `actor_id` | calculate | PAY-302 |
| `payrun.approved` | info | `pay_run_id`, `payouts`, `transfers`, `no_transfer`, `status`, `actor_id`, `duration_ms` | approve committed | PAY-400 |
| `payrun.approve_rejected` | info | `pay_run_id`, `code`, `status` | 409 `pay_run_not_approvable` | PAY-400 |
| `dual_approval.denied` | warning | `pay_run_id`, `payout_id?`, `action` (`approve` \| `retry`), `rule`, `employee_count`, `actor_id` | 403 `approver_must_differ` | PAY-401, PAY-500 |
| `payrun.completed` | info | `pay_run_id`, `status` (`paid` \| `partially_failed`), `paid`, `failed`, `first_completion: bool`, `settlement_s` (first only) | the run enters a final status (result, resolve or approve) | PAY-406 |
| `payrun.deleted` | info | `pay_run_id`, `status_at_delete`, `actor_id` | delete | PAY-304 |
| `payout.result_recorded` | info | `payout_id`, `pay_run_id`, `attempt`, `outcome`, `applied`, `attempt_status`, `payout_status`, `run_status`, `failure_reason?` | result 200 | PAY-405 |
| `payout.indeterminate` | warning | `payout_id`, `pay_run_id`, `attempt`, `publish_count` | outcome `indeterminate` applied | PAY-404, PAY-405 |
| `payout.attempt_not_current` | info | `payout_id`, `attempt`, `current_attempt`, `source` (`instruction` \| `result`) | 409 | PAY-405, PAY-411 |
| `payout.retry_requested` | info | `payout_id`, `pay_run_id`, `attempt` (new), `actor_id` | retry committed | PAY-500 |
| `payout.retry_rejected` | info | `payout_id`, `code` | retry 409 | PAY-500 |
| `payout.republished` | info | `payout_id`, `attempt`, `republish_seq`, `event_id`, `attempt_status` | re-publish row written | PAY-407 |
| `payout.operator_resolved` | warning | `payout_id`, `pay_run_id`, `attempt`, `action` (`redrive` \| `mark_rejected`), `note_len`, `actor_id`, `event_id` (redrive only) | resolve committed | PAY-502, R8 |
| `payout.resolve_rejected` | info | `payout_id`, `code` (`payout_not_blocked`, `resolve_too_early`), `attempt_status` | resolve 409 | PAY-502 |
| `payout.needs_operator` | error | `payout_id`, `pay_run_id`, `attempt`, `reason` (`poison`, `republish_limit`, or the worker's reason such as `key_window_expired`, `amount_mismatch`) | attempt → `needs_operator` | PAY-407, PAY-409, PAY-410 |
| `payout.instruction_served` | debug | `payout_id`, `attempt` | instruction 200 | PAY-411 |
| `crypto.decrypt_failed` | error | `kid`, `entity` (`employee` \| `payout_attempt`), `entity_id` | `FieldDecryptError` | PAY-204 |
| `idempotency.replayed` | info | `route`, `status` | stored response replayed | PAY-400 |
| `idempotency.key_reused` | warning | `route` | 422 `idempotency_key_reused` | PAY-400 |
| `outbox.relay.error` | error | `error_type`, `batch_size`, `failed`, `backoff_ms` | XADD or batch failure | PAY-402 |
| `outbox.relay.published` | debug | `count`, `duration_ms` | batch sent | PAY-402 |
| `maintenance.task.completed` (pycommon) | info | `task` (`republish`, `purge_outbox`, `purge_idempotency_keys`, `purge_bank_details`), `affected`, `duration_ms` | each task per tick | PAY-407, PAY-608, C44 |
| `maintenance.task.failed` (pycommon) | error | `task`, `error_type` (with traceback) | a task raised | PAY-608, C44 |
| `maintenance.skipped` (pycommon) | debug | — | advisory lock held by another replica | PAY-608 |
| `metrics.gauge_query_failed` | warning | `gauge`, `error_type` | collection callback failed | R26 |
| `startup.check_failed` (pycommon) | error (uat, prod; then exit 2) / warning (dev) | `setting`, `reason` | §6.2 | PAY-605, C29 |

pycommon also emits `http.request`, `http.error`, `http.unhandled`, `auth.token.rejected`, `auth.forbidden`, `settings.invalid`, `service.started`, `service.stopped`, `background.task.crashed`, `health.check_failed`, `ratelimit.backend_error`, `db.timeout` and `db.unavailable`.

### 7.2 Metrics

- **Naming:** the names below are the **exact Prometheus names**. Instruments follow the platform naming rule (platform design §3.3): counters are created without the `_total` suffix (the Collector appends it), histograms and seconds gauges with their full name and unit `s`. Counters and count gauges use annotation units in braces (`{run}`, `{payout}`, `{attempt}`, `{event}`), so no unit suffix is added. A unit test checks the exported names through `InMemoryMetricReader` plus the Prometheus name translation used by the Collector; `scripts/ci/check_metrics.sh` confirms them in Prometheus.
- **Labels:** every metric also carries `service_name` and `deployment_environment_name` from the resource.

| Metric | Type | Labels (values) | Recorded | Requirement |
|---|---|---|---|---|
| `payroll_pay_runs_total` | counter | `outcome` (`calculated`, `approved`, `paid`, `partially_failed`) | calculate; approve; **each** transition into a final status **[decision]** | plan §11, PAY-NFR-10 |
| `payroll_payouts_total` | counter | `outcome` (`paid`, `failed`, `indeterminate`, `needs_operator`, `no_transfer`, `operator_rejected`) | an applied result (settled → `paid`, rejected → `failed`, indeterminate, poison → `needs_operator`); a republish-limit transition; approve (`no_transfer`); an operator mark_rejected (`operator_rejected`, R8) | plan §11 |
| `payroll_payout_settlement_seconds` | histogram, buckets `pycommon.otel.SETTLEMENT_BUCKETS` = `[1, 2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 300, 600, 1800, 3600]` | — | once per run, when `first_final_at` is set: `first_final_at − approved_at` | PAY-406, PAY-NFR-06 |
| `payroll_runs_processing_oldest_age_seconds` | observable gauge, **collection-time** | — | `SELECT COALESCE(EXTRACT(EPOCH FROM now() - min(processing_since)), 0) FROM pay_runs WHERE status='processing' AND deleted_at IS NULL` | plan §11, PAY-NFR-11 |
| `payroll_payouts_stuck` | observable gauge, **collection-time** | — | `SELECT count(*) FROM payout_attempts WHERE status IN ('requested','indeterminate','needs_operator') AND requested_at < now() - make_interval(secs => :PAYOUT_STUCK_AFTER_S)` | plan §11, PAY-NFR-11 |
| `payroll_attempts_needs_operator` | observable gauge, **collection-time** **[decision]** | — | `SELECT count(*) FROM payout_attempts WHERE status='needs_operator'` | plan §11, PAY-409 |
| `payroll_payouts_republished_total` | counter | — | each re-publish outbox row | PAY-407 |
| `outbox_unsent` | observable gauge, collection-time | — | `SELECT count(*) FROM outbox WHERE sent_at IS NULL` | PAY-402, observability §5 |
| `outbox_oldest_age_seconds` | observable gauge, collection-time | — | `SELECT COALESCE(EXTRACT(EPOCH FROM now() - min(created_at)), 0) FROM outbox WHERE sent_at IS NULL` | PAY-402 (`OutboxStuck`) |
| `rate_limiter_errors_total` | counter (pycommon) | `component` | Redis error in the limiter (fail open) | PAY-603 |
| `rate_limited_total` | counter (pycommon) | `scope` (`caller`; C55) | 429 | PAY-603 |
| `maintenance_runs_total` / `maintenance_duration_seconds` | counter / histogram (pycommon) | `task`, `outcome` / `task` | each maintenance task per tick | PAY-608 |
| `http_server_request_duration_seconds` | histogram (auto) | `http_route`, `http_request_method`, `http_response_status_code` | every request | PAY-NFR-04 |
| DB pool in use / Redis connections | gauge (pycommon) | — | — | observability §5 |

**How the collection-time gauges work** (R26, C18; this section is authoritative for payroll's gauges, including `outbox_*`).
- **Callbacks:** `metrics.register_collection_gauges(sync_engine)` registers one OTel observable-gauge callback per gauge on the `payroll` meter. pycommon provides only the meter and the callback helper; the queries are payroll's.
- **Where they run:** `PeriodicExportingMetricReader` collects on its own thread every `OTEL_METRIC_EXPORT_INTERVAL` (15 s). The callbacks run there, against a **sync** psycopg engine (pool size 1, `statement_timeout = PAYROLL_GAUGE_QUERY_TIMEOUT_MS`), so they never block the event loop.
- **Result:** every replica exports its own fresh value at each collection. No replica exports a stale value, and the alerts use `max()`.
- **On failure:** a failed query logs `metrics.gauge_query_failed` and yields no observation for that cycle. It never yields a stale or zero value.

Each callback runs its own single-row query; five small queries every 15 s per replica.

### 7.3 Manual spans

| Span | Kind | Attributes | Where |
|---|---|---|---|
| `payroll.pay_run.create` | internal | `payroll.pay_run_id`, `payroll.schedule`, `payroll.employee_count` | services/create_run |
| `payroll.pay_run.hours` | internal | `payroll.pay_run_id`, `payroll.entries` | services/hours |
| `payroll.pay_run.calculate` | internal | `payroll.pay_run_id`, `payroll.payslips` | services/calculate |
| `payroll.pay_run.approve` | internal | `payroll.pay_run_id`, `payroll.payouts`, `payroll.transfers`, `payroll.idempotent_replay` | services/approve |
| `payroll.payout.retry` | internal | `payroll.payout_id`, `payroll.attempt` | services/retry |
| `payroll.payout.resolve` | internal | `payroll.payout_id`, `payroll.attempt`, `payroll.action` | services/resolve |
| `payroll.payout.instruction` | internal | `payroll.payout_id`, `payroll.attempt`, `payroll.allowed` | services/instruction |
| `payroll.payout.result` | internal | `payroll.payout_id`, `payroll.attempt`, `payroll.outcome`, `payroll.decision` | services/record_result |
| `payroll.outbox.publish` | producer | `messaging.system=redis`, `messaging.destination.name=stream:payout.requested`, `messaging.message.id`, `payroll.payout_id`, `payroll.attempt` | relay; parent = the envelope's stored traceparent |
| `payroll.maintenance.republish` | internal | `payroll.republished`, `payroll.needs_operator` | events/republish |
| `payroll.maintenance.purge` | internal | `payroll.outbox_purged`, `payroll.idempotency_purged`, `payroll.bank_purged` | maintenance |

Span attributes follow the same deny-list as logs. No amounts or names are recorded.

---

## 8. Authorization

### 8.1 Role matrix (PAY-601, plan §8, ADR 0002)

"any" = the token's `roles` intersects `{admin, payroll_admin, approver}`. A token whose roles miss the
requirement gets 403 `forbidden_role`. Roles are additive. The "worker" column is an `iss=payout-worker`
token; a `sub=service:web` token gets 403 `forbidden_role` on every route of this table (C30, §8.2).

| Endpoint | admin | payroll_admin | approver | worker |
|---|---|---|---|---|
| `GET /v1/company/settings` | ✓ | ✓ | ✓ | 401 |
| `PUT /v1/company/settings` | ✓ | 403 | 403 | 401 |
| `GET /v1/employees`, `GET /v1/employees/{id}` | ✓ | ✓ | ✓ | 401 |
| `POST /v1/employees`, `PATCH /v1/employees/{id}` | 403 | ✓ | 403 | 401 |
| `GET /v1/pay-runs`, `GET /v1/pay-runs/{id}` | ✓ | ✓ | ✓ | 401 |
| `POST /v1/pay-runs`, `DELETE /v1/pay-runs/{id}`, `PUT …/hours`, `POST …/calculate` | 403 | ✓ | 403 | 401 |
| `POST /v1/pay-runs/{id}/approve` | 403 | 403 | ✓ (+ PAY-401) | 401 |
| `GET …/payslips[/{payslip_id}]`, `GET …/payouts` | ✓ | ✓ | ✓ | 401 |
| `POST …/payouts/{payout_id}/retry` | 403 | 403 | ✓ (+ PAY-500 dual) | 401 |
| `POST …/payouts/{payout_id}/resolve` | 403 | 403 | ✓ (PAY-502) | 401 |
| `GET /v1/audit-events` | ✓ | 403 | 403 | 401 |
| `GET /v1/internal/payouts/{id}/instruction` | 401 | 401 | 401 | ✓ |
| `POST /v1/internal/payouts/{id}/result` | 401 | 401 | 401 | ✓ |

Implementation:
- Each router declares `dependencies=[Depends(require_web_user)]`, and each route adds `Depends(require_roles(...))`.
- The internal router declares `dependencies=[Depends(require_worker)]`.
- A test enumerates `app.routes` and fails if any non-health route lacks one of the two guards.

### 8.2 Issuer rules (PAY-600, PAY-601, PAY-411, R9, R34, C30)

Token verification is pycommon's (`authenticate`, attached by `create_app` to every non-health router):
a missing, malformed or expired token, a wrong `aud`, an unknown kid, a bad signature or an
issuer/kid mismatch → 401 `invalid_token` (C30). The route guards then apply:

- **`require_web_user`** (every public router):
  - `principal.iss != "web"` (for example a worker token on a public route) → 401 `invalid_token` (R34).
  - `iss == "web"` but `sub` is not a user UUID (`sub = service:web`, the public-flow token) → 403 `forbidden_role`: a valid token of the wrong kind for the route (C30).
  - `roles` must be a subset of the three known roles, else 403 `forbidden_role`.
  - `principal.name` (the `name` claim, `str | None`; R9, C10) is passed to calculate and approve for `calculated_by_name` and `approved_by_name`.
- **`require_worker`** (the internal router):
  - `principal.iss != "payout-worker"` (for example a web token) → 401 `invalid_token` (PAY-411).
  - `iss == "payout-worker"` but `sub != "service:payout-worker"` → 403 `forbidden_role` (C30).
  - Worker tokens carry `roles: []`, a `jti` and no `sid` (R9); payroll requires neither `sid` nor roles for them.
  - The kid-to-issuer binding is checked earlier by pycommon (PAY-600). So a web key can't mint `iss=payout-worker`, and the worker key can't mint `iss=web`.
- **Role check:** `require_roles(...)` → 403 `forbidden_role` (PAY-601).
- **`cid` scoping:** every repository call receives `principal.cid` and adds `company_id = :cid` to every statement, including the internal routes (the worker's `cid` comes from the event's `company_id`).
  - A resource in another company is indistinguishable from a missing one: 404 with the resource's `*_not_found` code.
  - `cid` is never read from a path, query or body.
- **Rate limit:** pycommon `caller_rate_limit(exempt_subs=("service:web",))` (C17), per `(iss, sub)` with pycommon's key format (prefix `payroll:rl`). `SERVICE_RATE_LIMIT_WORKER` applies when `iss == "payout-worker"`, and `SERVICE_RATE_LIMIT_USER` otherwise. The limiter fails open (PAY-603).

---

## 9. Tests

- Every test names the requirement it proves in its docstring.
- Unit tests run with `pytest-socket --disable-socket --allow-unix-socket`.
- Integration tests use Testcontainers Postgres 16 and Redis 7 through the pycommon fixtures (`pg_bootstrapped`, `redis_acl`, `token_factory`). The fixture runs the shared bootstrap SQL and migrates as `payroll_migrator`; the tests connect as the runtime role `payroll_app` and the Redis ACL user `payroll` (C1, C2).
- Coverage must be ≥ 85% (PAY-NFR-12).

### 9.1 `services/payroll/tests/unit/`

| File | Covers | Requirement |
|---|---|---|
| `test_periods.py` | Monthly start-on-the-1st rule, month-end (including February in leap years), index = month; biweekly anchor ± 14k (dates before and after the anchor), `period_end = start + 13`, index wraps at 26; `InvalidPeriodStart`; `pay_date = period_end`; hours bounds per schedule | PAY-300, PAY-301 |
| `test_overlap.py` | `periods_overlap` inclusive edges; `find_overlapping_employees` (a monthly run vs biweekly coverage, no overlap on adjacent days, de-duplication, sorting) | PAY-300 |
| `test_calc.py` | Worked examples: salary 5,200,001 cents over 26 periods; hourly half-up (e.g. 1,999 × 150); tax and retirement half-up boundaries at .5 cents; net = 0 for hours 0; `HoursMissing` lists every missing ID | PAY-302 |
| `test_calc_properties.py` | **Hypothesis:** (a) gross = net + tax + retirement and net ≥ 0 for any salary, rate, hours, `tax_bps ≤ 5000` and `retirement_bps ≤ 2000`; (b) Σ `salaried_gross(annual, k, n)` over k = 1..n == annual for n ∈ {12, 26}; (c) `run_totals(...).net_cents == Σ net` and gross total = Σ gross; and biweekly `period_index` is periodic in 26 | PAY-NFR-01, PAY-302 |
| `test_no_float.py` | AST scan of `src/payroll/domain/**`: no `float(` call, no float literal, no `/` true division, no `Decimal` | PAY-NFR-01 |
| `test_run_status.py` | `run_status_from_payouts` table plus a hypothesis property (any multiset of statuses → the plan's rule); empty input raises; `payout_status_for_attempt` | PAY-406 |
| `test_state_transitions.py` | Every row and column of the `decide_result` table (§3.3), including `needs_operator`; `instruction_allowed` for every status × current/stale; `assert_run_mutable` and `assert_run_approvable` per status | PAY-405, PAY-411, PAY-301, PAY-304, PAY-400 |
| `test_retry_rules.py` | `check_retry` order and each error (indeterminate, needs_operator, requested, paid → not retryable; `current_attempt` = 5 → limit; inactive or purged → bank missing); `republish_backoff_s` sequence 360, 720, 900, 900 at the defaults (R1); `republish_decision` (not due, due, unsent outbox, non-current, the boundary at 10 re-publishes → NEEDS_OPERATOR) | PAY-500, PAY-407 |
| `test_resolve_rules.py` | `check_resolve`: only a `requested` payout whose current attempt is `needs_operator` passes; every other attempt status (`requested`, `indeterminate`, `settled`, `rejected`) → `PayoutNotBlocked`; `resolve_transition` for both actions (redrive: `requested`, reason cleared, `publish_count = 1`, publish with `republish_seq = 0`; mark_rejected: `rejected`, `operator_rejected`, final, payout `failed`; mark_rejected before the 120 s wait → `ResolveTooEarly`, and redrive has no wait) | PAY-502 |
| `test_dual_approval.py` | Off → always passes; calculator == approver; a bank editor after `created_at` is blocked and one before it is allowed; several employees list all IDs; retry dual rule | PAY-401, PAY-500 |
| `test_payout_model.py` | **Hypothesis `RuleBasedStateMachine`** (§9.4). Owned by payroll only (C44) | PAY-NFR-02, PAY-406, PAY-405, PAY-411, PAY-500, PAY-502 |
| `test_field_crypto.py` | Round trip; token format `kid:nonce:ct`; a fresh nonce per call; a tampered ciphertext, nonce, kid or AAD (swapping between employees) raises `FieldDecryptError`; an unknown kid raises; decrypt with an old kid after rotation; bad key length refused; the error message holds no plaintext | PAY-204, ADR 0004 |
| `test_validation.py` | ABA checksum (valid and invalid), account 4–17 digits, salary and rate bounds, the settings ranges, a non-Monday anchor, a bad timezone, `HoursIn` duplicate IDs, `ResultIn` per-outcome field rules and `bank_reference` ≤ 64 (C26), `ResolveIn` (unknown action, empty note, 501-character note), `EmployeeUpdate` with no fields; every 422 has `details = [{field, issue}]` with no input value (C14) | PAY-200, PAY-201, PAY-301, PAY-405, PAY-502, PAY-602 |
| `test_authz_matrix.py` | Parametrised over every route (resolve included) × {admin, payroll_admin, approver, no-role, web token on an internal route → 401, worker token on a public route → 401, `sub=service:web` on a public route → 403, worker-issued token with another `sub` on an internal route → 403, bad signature / expired / issuer-kid mismatch → 401}: the exact status and code of §8.1–§8.2 (C30); every non-health route has a guard | PAY-601, PAY-411, PAY-600 |
| `test_errors.py` | Each `DomainError` maps to the status, code and details shape of §3.7 (including `anchor_locked` and `payout_not_blocked`); `ErrorCode` equals the list in §4.1; validation details are a `[{field, issue}]` list (C14); a 500 holds no traceback; `FieldDecryptError` → 500 `internal_error` plus the `crypto.decrypt_failed` log; 413 over `MAX_BODY_BYTES` | PAY-602, PAY-204, PAY-604 |
| `test_idempotency_hash.py` | Canonical hash stable across key order and whitespace; it differs by path parameter, route and body; scope composition | PAY-400 |
| `test_cursor.py` | Encode and decode round trip; a garbage, wrong-version or non-UUID cursor → `validation_error` with `details = [{field: "query.cursor", issue: "invalid_cursor"}]` | PAY-202, PAY-303, PAY-602 |
| `test_event_envelope.py` | `build_payout_requested` output validates against the schema; `traceparent` is taken from the active span, or synthesised when there is none; the schema rejects extra properties, a wrong currency, amount 0 and a negative `republish_seq` | PAY-402 |
| `test_settings.py` | Settings load the pycommon names (`DB_*`, `REDIS_*`, `DEBUG`, `DOCS_ENABLED`, `MAX_BODY_BYTES`; C1) and the `PAYROLL_*` keys; `LOG_LEVEL=info` is accepted as `INFO` (C24); startup checks through `run_startup_checks`: a `dev-` field kid in uat → `startup.check_failed` and exit 2 (C29); wrong key length; an active kid missing; `DOCS_ENABLED` on in prod; `PAYOUT_REPUBLISH_AFTER_S` = 360 passes and 300 fails the timing check (R1) → exit 2 in uat, warning in dev; logs the setting name and never the value | PAY-605 |
| `test_metrics.py` | `InMemoryMetricReader`: each business counter and histogram increments with the right labels on its path (including `payroll_payouts_total{outcome="operator_rejected"}`); the settlement histogram is observed once per run; exported Prometheus names are exact | PAY-NFR-10, PAY-406 |
| `test_log_redaction.py` | Every §7.1 event emitted on its path holds no key or value matching the salary, amount, employee or staff name, email, routing, account or resolve-note values used in the test | PAY-NFR-07 |

### 9.2 `services/payroll/tests/integration/`

| File | Covers | Requirement |
|---|---|---|
| `conftest.py` | Containers, bootstrap SQL, migrate as the migrator, runtime-role sessions, truncate and flush between tests, token factory (web roles, worker, wrong issuer), frozen clock | — |
| `test_migrations.py` | Upgrade to head, downgrade one step, upgrade again; `0002` seeds `company_settings` for `COMPANY_ID` with the defaults (timezone, anchor 2026-01-05, dual approval true); re-running is a no-op; a missing `COMPANY_ID` fails | PAY-200, PAY-606 |
| `test_grants.py` | `payroll_app` can't `SELECT` from `identity.users`, `payouts.disbursements` or `bank_stub.transfers` (`permission denied`), can't run DDL in `payroll`, and can read but not write `payroll.alembic_version` | PAY-606 |
| `test_settings_api.py` | GET and PUT; out-of-range 422 with `[{field, issue}]` details; `settings.updated` audit with the changed fields; admin-only PUT; changing `biweekly_anchor` with a non-deleted biweekly run → 409 `anchor_locked` and no change, allowed again once that run is deleted, and allowed with only monthly runs; a PUT racing a biweekly create never leaves a run on the old anchor (R33) | PAY-200 |
| `test_employees_api.py` | Create, list (cursor, status filter, order), get, patch; `employee_email_taken` (case-insensitive); another company → 404; responses never contain the routing or account digits (scanned); `bank_updated_by` and `bank_updated_at` set; audit diff has only field names and `account_last4`; reactivation after purge needs bank details | PAY-201, PAY-202, PAY-203, PAY-NFR-08 |
| `test_pay_run_create.py` | Monthly and biweekly creation; `pay_run_exists`; recreate after delete succeeds; `invalid_period_start`; `no_employees`; `employee_in_overlapping_run` after a schedule change (with IDs); inactive employees excluded; snapshot fields; two concurrent creates for the same period → one 201 and one 409 | PAY-300 |
| `test_hours_calculate.py` | Hours upsert returns `HoursOut` (C7); `not_hourly`; `employee_not_in_run`; bounds per schedule; a calculated run reverts to draft and its payslips reset (`calculated_by_name` cleared); `hours_missing`; calculate snapshots the rates and dual flag and stores `calculated_by_name` from the token's `name` (NULL without the claim); totals = Σ stored payslips; summary has exactly the six `counts` keys, `require_dual_approval`, `calculated_by_name`, `approved_by_name` and `funding_required_cents` (C9); the payslips list pages with `cursor`/`limit` until `next_cursor` is null (C8); `pay_run_locked` after approval; delete only in draft or calculated | PAY-301, PAY-302, PAY-303, PAY-304 |
| `test_approve.py` | Approve creates payouts, attempt 1, snapshots and outbox rows in one transaction; `approved_by_name` stored from the token; a net = 0 payout → `paid` + `no_transfer` with no outbox row; an all-zero run → `paid` straight away; dual approval (calculator, bank editor after `created_at`, creator of the employee counts as a bank editor); setting off after calculate keeps the snapshot; missing or purged bank details → 409 `employee_bank_details_missing` (C40); rollback on failure leaves no payouts or outbox rows | PAY-400, PAY-401, PAY-402 |
| `test_approve_concurrency.py` | Two parallel approves with different keys → exactly one 200 and one 409 `pay_run_not_approvable`, one set of payouts; two parallel with the **same** key → both 200 with the same body, one set of payouts | PAY-400 |
| `test_idempotency.py` | Replay returns the stored response with `Idempotent-Replayed`; same key with a different body → 422 `idempotency_key_reused`; same key on another run or from another user → treated as a new key (scope §4.8, C60); an expired key (clock + 25 h) is treated as new; a missing key on approve and on result → 400 `idempotency_key_required`, a malformed key → 422 `validation_error` (C34); result-key semantics (`<event_id>:indeterminate`, then `<event_id>:settled` both apply) | PAY-400, PAY-405 |
| `test_internal_instruction.py` | 200 shape and decrypted values for a current requested or indeterminate attempt; 409 for stale, final and `needs_operator`; 404 for another `cid`; 401 for a web token, 403 for a worker-issued token with the wrong `sub` (C30); `Cache-Control: no-store` | PAY-411 |
| `test_internal_result.py` | The response is exactly `{payout_id, attempt, outcome}` with `outcome` = the stored attempt status (R7, C26); each outcome → attempt, payout and run state; already final → 200 with the stored status and no change; stale → 409; `indeterminate` after `settled` → unchanged; `settled` after `indeterminate` → applied; poison → `needs_operator` and re-publishing stops; on a `needs_operator` attempt `settled` and `rejected` apply while `indeterminate` and `poison` return 200 unchanged; a 65-character `bank_reference` → 422; run status equals the function after every call; `processing_since` and `first_final_at` behaviour; the settlement histogram is observed once | PAY-405, PAY-406, PAY-409 |
| `test_retry.py` | Retry after rejected → attempt 2 with the new bank snapshot, outbox row, run back to `processing`; `payout_not_retryable` (requested, indeterminate, needs_operator, paid); `retry_limit_reached` at attempt 5; `employee_bank_details_missing` (inactive, purged); dual approval vs `bank_updated_by` using the run's snapshot; a late result for attempt 1 after the retry → 409 `attempt_not_current`; retry racing a late result (parallel tasks) → consistent final state; the payout list (paginated, C8) shows `payslip_id`, `amount_cents` and every attempt with `account_last4` only | PAY-500, PAY-501, PAY-NFR-02 |
| `test_resolve.py` | A `needs_operator` attempt (poison or republish limit): **redrive** → attempt `requested`, `failure_reason` NULL, `publish_count = 1`, `last_published_at` = now, `requested_at` unchanged, a new outbox row with a fresh `event_id` and `republish_seq = 0` (C13), then a `settled` result → payout `paid`; **mark_rejected** → attempt `rejected` / `operator_rejected`, payout `failed`, run final when it was the last open payout (`first_final_at`, histogram once), then a retry → attempt 2 → `paid` (with `needs_operator_at` set more than 120 s back via time-machine); mark_rejected inside the wait → 409 `resolve_too_early` with `Retry-After`; 409 `payout_not_blocked` for `requested`, `indeterminate`, `settled`, `rejected` and `no_transfer` payouts; 404 for a payout of another run or company; 422 for an unknown action or a missing note; audit `payout.operator_resolved` holds `note_len` and never the note; a resolve racing a late result (parallel tasks) → one consistent outcome (lock order §4.10); approver only | PAY-502 |
| `test_outbox.py` | The state change and the outbox row commit together, and a forced rollback leaves neither; the relay publishes through `StreamProducer.publish` with exactly the fields `event_id`, `type` and `envelope` (R7, C22), `MAXLEN ~`, marks the row sent and updates `last_published_at`; two relays in parallel never send a row twice (SKIP LOCKED); Redis down → rows stay unsent, `outbox.relay.error`, catch-up after Redis is back; the published envelope validates against the schema; `traceparent` continues the approve trace (in-memory span exporter) | PAY-402, PAY-NFR-09 |
| `test_republish.py` | Not due → nothing; due (frozen clock) → a new outbox row with a fresh `event_id`, `republish_seq = publish_count` before the increment (R34), the count incremented; backoff 360, 720, 900, 900 (R1); after a redrive the backoff restarts at 360; an unsent outbox row blocks re-publish; after 10 re-publishes → `needs_operator` and the audit row; stream FLUSHALL, then a tick re-publishes every non-final attempt; indeterminate attempts are re-published with the same `(payout_id, attempt)` | PAY-407, PAY-NFR-03 |
| `test_maintenance.py` | Advisory lock `payroll:maintenance`: two runners → only one ticks; each task logs `maintenance.task.completed` (or `maintenance.task.failed` without stopping the others; C44); sent outbox rows older than 7 days purged and unsent kept; idempotency keys past `expires_at` purged; bank purge after 30 days of deactivation nulls the employee fields and the final attempts' snapshots, **skips** an employee with a non-final attempt, and writes the audit row; a draft run with a purged employee → approve 409 | PAY-608, PAY-407 |
| `test_collection_gauges.py` | `payroll_runs_processing_oldest_age_seconds`, `payroll_payouts_stuck`, `payroll_attempts_needs_operator`, `outbox_unsent` and `outbox_oldest_age_seconds` computed at collection on two app instances against the same DB give the same fresh values; zero when nothing matches; DB down → no observation plus a warning | plan §2.1, §11; PAY-NFR-11 |
| `test_rate_limit.py` | Per-caller limit shared across two app instances (Redis) → 429 with `Retry-After`; the worker uses the worker limit; Redis down → fail open, `rate_limiter_errors_total` incremented | PAY-603 |
| `test_readyz.py` | `/readyz` fails when the DB or Redis is down, or when migrations aren't at head; `/livez` stays up; a uat-mode start with a placeholder secret exits 2 before connecting (C29) | PAY-605 |
| `test_secret_not_logged.py` | With captured logs over a full create → calculate → approve → result → retry → resolve flow: no field-key bytes, JWT, routing or account number, salary value, or employee name or email appears | PAY-NFR-07 |

### 9.3 `services/payroll/tests/contract/`

| File | Covers | Requirement |
|---|---|---|
| `test_openapi_snapshot.py` | `scripts/export_openapi.py payroll` output equals the committed `openapi.json` (CI also runs the git-diff drift job); every route documents its error responses with the shared error schema | PAY-NFR-12 |
| `test_event_schema.py` | `contracts/events/payout.requested.v1.json` is a valid draft 2020-12 schema; the §5.1 example (dev `COMPANY_ID`, C28) validates; mutated examples fail (extra property in the envelope or `data`, `version: 2`, `type` wrong, `producer` wrong, currency not USD, amount 0, a non-UUID ID, a missing or bad `traceparent`); `contracts/events/payout.requested.dlq.v1.json` (C21) is a valid draft 2020-12 schema and its example validates, with an `envelope` string that validates against the v1 schema | PAY-402, PAY-NFR-12 |
| `test_error_codes.py` | Every code in §4.1 is reachable or documented in the OpenAPI spec, and none is outside the list | PAY-602 |

Schemathesis runs in CI against the running service (testing.md §4). It is not a file here.

### 9.4 Stateful payout model (`tests/unit/test_payout_model.py`, PAY-NFR-02)

The model drives the **real** domain functions (`decide_result`, `instruction_allowed`, `check_retry`,
`republish_decision`, `run_status_from_payouts`, `payout_status_for_attempt`) through an in-memory
harness that mirrors the services layer. It runs with no I/O.

- **State:**
  - a payroll store: payouts, attempts and run status;
  - a bank ledger `dict[key, Transfer]` with the outcome per key, chosen by a hypothesis strategy (settled or rejected), first write wins;
  - a stream: a list of in-flight messages `(event_id, payout_id, attempt, amount)`;
  - worker consumers holding a message at a step: `fetched_instruction`, `called_bank` (`bank_answered: bool`) or `posted_result`;
  - `processed_events`;
  - a clock.
- **Rules** (each `@rule`, with `@precondition`s):
  - `approve` (1–3 payouts, some net = 0);
  - `deliver(msg)` — start or redeliver, including a second consumer on the same message;
  - `fetch_instruction`;
  - `bank_call` (answers, times out after settling, or times out before settling);
  - `post_result(outcome)` (settled, rejected, indeterminate, poison, or a stale attempt);
  - `crash_consumer` (drops the in-flight state at any step; the message stays pending);
  - `ack`;
  - `exhaust_budget` → post indeterminate;
  - `advance_clock` plus `maintenance_tick` (re-publish with a fresh `event_id`; `needs_operator` after the maximum);
  - `retry(payout)` (only via `check_retry`);
  - `resolve(payout, action)` (only via `check_resolve`): `redrive`, or `mark_rejected` only after `PAYOUT_MARK_REJECTED_MIN_WAIT_S` since `needs_operator_at` (the model treats in-flight handlers as expired after `HANDLER_DEADLINE_S`) and with the runbook precondition that the attempt's key has no settled transfer;
  - `flush_stream` (Redis data loss);
  - `late_result_for_old_attempt`.
- **Invariants** (`@invariant`, checked after every step):
  1. Per `payout_id`: at most one bank key with a `settled` transfer across all attempts.
  2. `payout.status == paid` ⇔ (`no_transfer` and zero transfers) or exactly one settled transfer **and** the recorded attempt is that one.
  3. `payout.status == failed` ⇒ the current attempt's bank key has no settled transfer (it is `rejected` at the bank, or the attempt is `operator_rejected`).
  4. A retry never happens unless the current attempt is `rejected`; an `indeterminate` or `needs_operator` attempt is never retried. A resolve happens only on a `needs_operator` attempt.
  5. The stored run status == `run_status_from_payouts(payout statuses)` (PAY-406).
  6. A final attempt's status never changes.
  7. **Liveness under a healthy bank** (a `teardown` drain: deliver everything, the bank always answers, ticks until quiet): every payout is final or `needs_operator`.
- **Settings:** `max_examples=500` and `stateful_step_count=60` in CI; the nightly run uses 5000.

### 9.5 Traceability

| Requirement | Modules | Tests |
|---|---|---|
| PAY-200 | `api/settings_router.py`, `services/settings.py` (`anchor_locked`), `services/create_run.py` (settings `FOR SHARE`), migration `0002` | `unit/test_validation.py`, `unit/test_errors.py`, `integration/test_settings_api.py`, `integration/test_migrations.py` |
| PAY-201 | `api/employees_router.py`, `services/employees.py`, `domain/validation.py`, `crypto.py` | `unit/test_validation.py`, `integration/test_employees_api.py` |
| PAY-202 | `api/employees_router.py`, `api/pagination.py`, `db/repositories.py` | `unit/test_cursor.py`, `integration/test_employees_api.py` |
| PAY-203 | `services/employees.py` | `integration/test_employees_api.py` |
| PAY-204 | `crypto.py`, `api/errors.py` | `unit/test_field_crypto.py`, `unit/test_errors.py` |
| PAY-300 | `domain/periods.py`, `services/create_run.py` | `unit/test_periods.py`, `unit/test_overlap.py`, `integration/test_pay_run_create.py` |
| PAY-301 | `domain/periods.py`, `domain/state.py`, `services/hours.py` | `unit/test_periods.py`, `integration/test_hours_calculate.py` |
| PAY-302 | `domain/calc.py`, `services/calculate.py` | `unit/test_calc.py`, `unit/test_calc_properties.py`, `integration/test_hours_calculate.py` |
| PAY-303 | `api/pay_runs_router.py`, `api/schemas.py` (`PayRunOut.counts`, `*_name`), `api/pagination.py`, `db/repositories.py` | `unit/test_cursor.py`, `integration/test_hours_calculate.py` |
| PAY-304 | `domain/state.py`, `services/create_run.py` (delete) | `unit/test_state_transitions.py`, `integration/test_hours_calculate.py` |
| PAY-400 | `services/approve.py`, `api/deps.py` (idempotency), `domain/state.py` | `unit/test_idempotency_hash.py`, `integration/test_approve.py`, `integration/test_approve_concurrency.py`, `integration/test_idempotency.py` |
| PAY-401 | `domain/dual_approval.py`, `services/approve.py` | `unit/test_dual_approval.py`, `integration/test_approve.py` |
| PAY-402 | `events/envelope.py`, `events/outbox.py`, `events/relay.py`, `contracts/events/payout.requested.v1.json` | `unit/test_event_envelope.py`, `integration/test_outbox.py`, `contract/test_event_schema.py` |
| PAY-405 | `domain/state.py` (`decide_result`), `services/record_result.py`, `api/internal_router.py` | `unit/test_state_transitions.py`, `integration/test_internal_result.py`, `integration/test_idempotency.py` |
| PAY-406 | `domain/state.py` (`run_status_from_payouts`), `services/record_result.py`, `services/retry.py`, `services/resolve.py`, `services/approve.py` | `unit/test_run_status.py`, `unit/test_payout_model.py`, `integration/test_internal_result.py`, `integration/test_resolve.py` |
| PAY-407 | `domain/retry.py`, `events/republish.py`, `maintenance.py`, `settings.py` (timing check) | `unit/test_retry_rules.py`, `unit/test_settings.py`, `integration/test_republish.py` |
| PAY-409 | `domain/state.py`, `services/record_result.py` | `integration/test_internal_result.py` |
| PAY-410 | `services/instruction.py` (`requested_at`), `services/record_result.py` (poison reason) | `integration/test_internal_instruction.py`, `integration/test_internal_result.py` |
| PAY-411 | `api/internal_router.py`, `services/instruction.py`, `api/deps.py` | `unit/test_state_transitions.py`, `unit/test_authz_matrix.py`, `integration/test_internal_instruction.py` |
| PAY-500 | `domain/retry.py`, `domain/dual_approval.py`, `services/retry.py` | `unit/test_retry_rules.py`, `unit/test_dual_approval.py`, `integration/test_retry.py` |
| PAY-501 | `api/pay_runs_router.py` (payouts), `api/schemas.py` (`PayoutOut`) | `integration/test_retry.py` |
| PAY-502 | `domain/resolve.py`, `services/resolve.py`, `api/pay_runs_router.py` (resolve) | `unit/test_resolve_rules.py`, `unit/test_payout_model.py`, `unit/test_validation.py`, `integration/test_resolve.py` |
| PAY-600 | `api/deps.py` (pycommon JWT) | `unit/test_authz_matrix.py` |
| PAY-601 | `api/deps.py`, every router, `db/repositories.py` (`cid`) | `unit/test_authz_matrix.py`, `integration/test_employees_api.py` (cross-company 404) |
| PAY-602 | `api/errors.py`, `api/pagination.py` | `unit/test_errors.py`, `unit/test_validation.py`, `unit/test_cursor.py`, `contract/test_error_codes.py` |
| PAY-603 | pycommon limiter wiring in `main.py` | `integration/test_rate_limit.py` |
| PAY-604 | `main.py` (body limit) | `unit/test_errors.py` (413) |
| PAY-605 | `settings.py` (pycommon `run_startup_checks`), pycommon health | `unit/test_settings.py`, `integration/test_readyz.py` |
| PAY-606 | migrations, bootstrap SQL | `integration/test_grants.py`, `integration/test_migrations.py` |
| PAY-607 | `settings.py` (ACL user), relay | `integration/test_outbox.py` (connects as `payroll`) |
| PAY-608 | `maintenance.py` (pycommon `MaintenanceRunner`) | `integration/test_maintenance.py` |
| PAY-NFR-01 | `domain/calc.py`, `domain/periods.py` | `unit/test_calc_properties.py`, `unit/test_no_float.py` |
| PAY-NFR-02 | `domain/state.py`, `domain/retry.py`, services | `unit/test_payout_model.py`, `integration/test_retry.py`, `integration/test_internal_result.py` |
| PAY-NFR-03 | `events/republish.py` | `integration/test_republish.py`, `unit/test_payout_model.py` (liveness) |
| PAY-NFR-06 | `metrics.py` (settlement histogram) | `unit/test_metrics.py`, `integration/test_internal_result.py` |
| PAY-NFR-07 | logging calls, redactor | `unit/test_log_redaction.py`, `integration/test_secret_not_logged.py` |
| PAY-NFR-08 | `api/schemas.py` (`account_last4` only), `maintenance.py` | `integration/test_employees_api.py`, `integration/test_maintenance.py` |
| PAY-NFR-09 | `events/relay.py` (traceparent) | `integration/test_outbox.py` |
| PAY-NFR-10, PAY-NFR-11 | `metrics.py` | `unit/test_metrics.py`, `integration/test_collection_gauges.py` |
| PAY-NFR-12 | `openapi.json`, event schemas (`payout.requested.v1.json`, `payout.requested.dlq.v1.json`) | `contract/*` |

PAY-403, PAY-404, PAY-408 and PAY-412 are owned by payout-worker and bank-stub. Payroll's side of them is §4.6, §4.7 and §5.

---

## 10. Change log

- 2026-11-10: incorporated README R-table and C-table decisions (R1, R2, R3, R4, R7, R8, R9, R11, R13, R21, R22, R26, R27, R33, R34, R37; C1, C2, C5, C7, C8, C9, C10, C13, C14, C17, C18, C21, C22, C23, C24, C26, C28, C29, C30, C34, C40, C44).

**Open:**

1. *(Resolved by C48, 2026-11-10: `needs_operator_at` plus a 120 s minimum wait enforced by payroll; error `resolve_too_early`.)*
