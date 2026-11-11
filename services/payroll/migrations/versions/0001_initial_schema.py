"""Initial payroll schema: the 10 tables of design §2 with every CHECK and index.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-11-10
"""

# ruff: noqa: E501  (the DDL is verbatim from design §2)

from collections.abc import Sequence

from alembic import op

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# One statement per entry: asyncpg runs each as its own prepared statement.
UPGRADE: Sequence[str] = (
    # 2.1 company_settings
    """
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
    )
    """,
    # 2.2 employees
    """
    CREATE TABLE payroll.employees (
      id                  uuid        PRIMARY KEY,
      company_id          uuid        NOT NULL REFERENCES payroll.company_settings(company_id)
                                      ON DELETE RESTRICT,
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
      status              text        NOT NULL DEFAULT 'active'
                                      CHECK (status IN ('active','inactive')),
      deactivated_at      timestamptz NULL,
      created_by          text        NOT NULL,
      created_at          timestamptz NOT NULL DEFAULT now(),
      updated_at          timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT ck_employees_pay_basis CHECK (
        (pay_type = 'salaried' AND annual_salary_cents BETWEEN 1 AND 1000000000
                               AND hourly_rate_cents IS NULL)
     OR (pay_type = 'hourly'   AND hourly_rate_cents   BETWEEN 1 AND 100000
                               AND annual_salary_cents IS NULL)),
      CONSTRAINT ck_employees_deactivated CHECK ((status = 'inactive') = (deactivated_at IS NOT NULL)),
      CONSTRAINT ck_employees_bank_all_or_none CHECK (
        (bank_routing_enc IS NULL) = (bank_account_enc IS NULL)
        AND (bank_account_enc IS NULL) = (account_last4 IS NULL)),
      CONSTRAINT ck_employees_active_has_bank CHECK (status = 'inactive' OR bank_account_enc IS NOT NULL)
    )
    """,
    "CREATE UNIQUE INDEX uq_employees_company_email ON payroll.employees (company_id, lower(email))",
    "CREATE INDEX ix_employees_list ON payroll.employees (company_id, status, name, id)",
    "CREATE INDEX ix_employees_schedule ON payroll.employees (company_id, schedule, status)",
    """
    CREATE INDEX ix_employees_purge ON payroll.employees (deactivated_at)
      WHERE status = 'inactive' AND bank_account_enc IS NOT NULL
    """,
    # 2.3 pay_runs
    """
    CREATE TABLE payroll.pay_runs (
      id                     uuid        PRIMARY KEY,
      company_id             uuid        NOT NULL REFERENCES payroll.company_settings(company_id)
                                         ON DELETE RESTRICT,
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
        (total_gross_cents IS NULL AND total_tax_cents IS NULL AND total_retirement_cents IS NULL
         AND total_net_cents IS NULL)
     OR (total_gross_cents = total_net_cents + total_tax_cents + total_retirement_cents)),
      CONSTRAINT ck_pay_runs_calculated CHECK (
        status = 'draft' OR (calculated_by IS NOT NULL AND tax_bps IS NOT NULL
                             AND retirement_bps IS NOT NULL
                             AND require_dual_approval IS NOT NULL AND total_net_cents IS NOT NULL)),
      CONSTRAINT ck_pay_runs_approved CHECK (
        (status IN ('draft','calculated')) = (approved_at IS NULL)
        AND (approved_at IS NULL) = (approved_by IS NULL)),
      CONSTRAINT ck_pay_runs_processing CHECK (status <> 'processing' OR processing_since IS NOT NULL),
      CONSTRAINT ck_pay_runs_deleted CHECK (deleted_at IS NULL OR status IN ('draft','calculated'))
    )
    """,
    """
    CREATE UNIQUE INDEX uq_pay_runs_company_schedule_period
      ON payroll.pay_runs (company_id, schedule, period_start) WHERE deleted_at IS NULL
    """,
    """
    CREATE INDEX ix_pay_runs_list ON payroll.pay_runs (company_id, period_start DESC, id DESC)
      WHERE deleted_at IS NULL
    """,
    """
    CREATE INDEX ix_pay_runs_processing ON payroll.pay_runs (processing_since)
      WHERE status = 'processing'
    """,
    # 2.5 payslips (before pay_run_hours, whose FK references it)
    """
    CREATE TABLE payroll.payslips (
      id                  uuid        PRIMARY KEY,
      company_id          uuid        NOT NULL,
      pay_run_id          uuid        NOT NULL REFERENCES payroll.pay_runs(id) ON DELETE RESTRICT,
      employee_id         uuid        NOT NULL REFERENCES payroll.employees(id) ON DELETE RESTRICT,
      employee_name       text        NOT NULL,
      pay_type            text        NOT NULL CHECK (pay_type IN ('salaried','hourly')),
      annual_salary_cents bigint      NULL,
      hourly_rate_cents   bigint      NULL,
      hours_hundredths    integer     NULL,
      status              text        NOT NULL DEFAULT 'pending'
                                      CHECK (status IN ('pending','calculated')),
      gross_cents         bigint      NULL CHECK (gross_cents >= 0),
      tax_cents           bigint      NULL CHECK (tax_cents >= 0),
      retirement_cents    bigint      NULL CHECK (retirement_cents >= 0),
      net_cents           bigint      NULL CHECK (net_cents >= 0),
      created_at          timestamptz NOT NULL DEFAULT now(),
      updated_at          timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_payslips_run_employee UNIQUE (pay_run_id, employee_id),
      CONSTRAINT ck_payslips_basis CHECK (
        (pay_type = 'salaried' AND annual_salary_cents IS NOT NULL AND hourly_rate_cents IS NULL
                               AND hours_hundredths IS NULL)
     OR (pay_type = 'hourly'   AND hourly_rate_cents IS NOT NULL AND annual_salary_cents IS NULL)),
      CONSTRAINT ck_payslips_amounts CHECK (
        (status = 'pending' AND gross_cents IS NULL AND tax_cents IS NULL
                            AND retirement_cents IS NULL AND net_cents IS NULL)
     OR (status = 'calculated' AND gross_cents = net_cents + tax_cents + retirement_cents))
    )
    """,
    "CREATE INDEX ix_payslips_list ON payroll.payslips (pay_run_id, employee_name, id)",
    "CREATE INDEX ix_payslips_employee ON payroll.payslips (employee_id, pay_run_id)",
    # 2.4 pay_run_hours
    """
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
        REFERENCES payroll.payslips (pay_run_id, employee_id) ON DELETE RESTRICT
    )
    """,
    # 2.6 payouts
    """
    CREATE TABLE payroll.payouts (
      id              uuid        PRIMARY KEY,
      company_id      uuid        NOT NULL,
      pay_run_id      uuid        NOT NULL REFERENCES payroll.pay_runs(id) ON DELETE RESTRICT,
      payslip_id      uuid        NOT NULL UNIQUE REFERENCES payroll.payslips(id) ON DELETE RESTRICT,
      employee_id     uuid        NOT NULL REFERENCES payroll.employees(id) ON DELETE RESTRICT,
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
    )
    """,
    "CREATE INDEX ix_payouts_run_status ON payroll.payouts (pay_run_id, status)",
    "CREATE INDEX ix_payouts_employee ON payroll.payouts (employee_id)",
    # 2.7 payout_attempts
    """
    CREATE TABLE payroll.payout_attempts (
      id                uuid        PRIMARY KEY,
      company_id        uuid        NOT NULL,
      payout_id         uuid        NOT NULL REFERENCES payroll.payouts(id) ON DELETE RESTRICT,
      attempt           smallint    NOT NULL CHECK (attempt >= 1),
      status            text        NOT NULL DEFAULT 'requested'
                        CHECK (status IN ('requested','indeterminate','needs_operator',
                                          'settled','rejected')),
      failure_reason    text        NULL CHECK (failure_reason ~ '^[a-z0-9_]{1,100}$'),
      bank_reference    text        NULL CHECK (char_length(bank_reference) BETWEEN 1 AND 64),
      bank_snapshot_enc text        NULL,
      account_last4     char(4)     NOT NULL CHECK (account_last4 ~ '^[0-9]{4}$'),
      requested_by      text        NOT NULL,
      requested_at      timestamptz NOT NULL,
      last_published_at timestamptz NOT NULL,
      needs_operator_at timestamptz NULL,
      CONSTRAINT ck_attempt_needs_operator_at
        CHECK ((status = 'needs_operator') = (needs_operator_at IS NOT NULL)),
      publish_count     integer     NOT NULL DEFAULT 1 CHECK (publish_count >= 1),
      final_at          timestamptz NULL,
      created_at        timestamptz NOT NULL DEFAULT now(),
      updated_at        timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_payout_attempts_payout_attempt UNIQUE (payout_id, attempt),
      CONSTRAINT ck_attempts_final CHECK ((status IN ('settled','rejected')) = (final_at IS NOT NULL)),
      CONSTRAINT ck_attempts_rejected_reason CHECK (status <> 'rejected' OR failure_reason IS NOT NULL),
      CONSTRAINT ck_attempts_settled_ref CHECK (status <> 'settled' OR bank_reference IS NOT NULL),
      CONSTRAINT ck_attempts_snapshot
        CHECK (status IN ('settled','rejected') OR bank_snapshot_enc IS NOT NULL)
    )
    """,
    """
    CREATE INDEX ix_attempts_republish ON payroll.payout_attempts (last_published_at)
      WHERE status IN ('requested','indeterminate')
    """,
    """
    CREATE INDEX ix_attempts_nonfinal ON payroll.payout_attempts (requested_at)
      WHERE status IN ('requested','indeterminate','needs_operator')
    """,
    # 2.8 audit_events
    """
    CREATE TABLE payroll.audit_events (
      id          uuid        PRIMARY KEY,
      company_id  uuid        NOT NULL,
      actor_id    text        NOT NULL,
      event       text        NOT NULL CHECK (event ~ '^[a-z_]+\\.[a-z_]+$'),
      entity_type text        NOT NULL
                              CHECK (entity_type IN ('company_settings','employee','pay_run','payout')),
      entity_id   uuid        NOT NULL,
      details     jsonb       NOT NULL DEFAULT '{}'::jsonb,
      request_id  text        NULL,
      trace_id    text        NULL,
      created_at  timestamptz NOT NULL DEFAULT now(),
      updated_at  timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_audit_events_list ON payroll.audit_events (company_id, id DESC)",
    """
    CREATE INDEX ix_audit_events_entity
      ON payroll.audit_events (company_id, entity_type, entity_id, id DESC)
    """,
    # 2.9 outbox
    """
    CREATE TABLE payroll.outbox (
      id              uuid        PRIMARY KEY,
      company_id      uuid        NOT NULL,
      event_type      text        NOT NULL CHECK (event_type = 'payout.requested'),
      event_version   smallint    NOT NULL CHECK (event_version = 1),
      payload         jsonb       NOT NULL,
      payout_id       uuid        NULL,
      attempt         smallint    NULL,
      sent_at         timestamptz NULL,
      relay_attempts  integer     NOT NULL DEFAULT 0,
      last_error      text        NULL,
      created_at      timestamptz NOT NULL DEFAULT now(),
      updated_at      timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_outbox_unsent ON payroll.outbox (created_at, id) WHERE sent_at IS NULL",
    """
    CREATE INDEX ix_outbox_unsent_attempt ON payroll.outbox (payout_id, attempt)
      WHERE sent_at IS NULL
    """,
    "CREATE INDEX ix_outbox_sent ON payroll.outbox (sent_at) WHERE sent_at IS NOT NULL",
    # 2.10 idempotency_keys
    """
    CREATE TABLE payroll.idempotency_keys (
      company_id      uuid        NOT NULL,
      scope           text        NOT NULL,
      key             text        NOT NULL CHECK (key ~ '^[A-Za-z0-9._:-]{1,200}$'),
      request_hash    char(64)    NOT NULL,
      response_status smallint    NOT NULL,
      response_body   jsonb       NOT NULL,
      expires_at      timestamptz NOT NULL,
      created_at      timestamptz NOT NULL DEFAULT now(),
      updated_at      timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (company_id, scope, key)
    )
    """,
    "CREATE INDEX ix_idempotency_expires ON payroll.idempotency_keys (expires_at)",
)

TABLES_IN_CREATE_ORDER: Sequence[str] = (
    "company_settings",
    "employees",
    "pay_runs",
    "payslips",
    "pay_run_hours",
    "payouts",
    "payout_attempts",
    "audit_events",
    "outbox",
    "idempotency_keys",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for table in reversed(TABLES_IN_CREATE_ORDER):
        op.execute(f"DROP TABLE payroll.{table}")
