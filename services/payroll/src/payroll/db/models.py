"""SQLAlchemy 2.0 models of the 10 payroll tables (design §2).

The DDL itself (CHECKs, partial indexes) is emitted verbatim by migration ``0001``; these models
describe the columns for queries. Timestamps are ``timestamptz``; ``updated_at`` is set by the ORM
and Core updates (``onupdate``), never by triggers.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "AuditEvent",
    "Base",
    "CompanySettings",
    "Employee",
    "IdempotencyKey",
    "Outbox",
    "PayRun",
    "PayRunHours",
    "Payout",
    "PayoutAttempt",
    "Payslip",
]

SCHEMA: Final = "payroll"


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


def _uuid(*, primary_key: bool = False, nullable: bool = False) -> Any:
    return mapped_column(PG_UUID(as_uuid=True), primary_key=primary_key, nullable=nullable)


def _ts(*, nullable: bool = True) -> Any:
    return mapped_column(DateTime(timezone=True), nullable=nullable)


class _Timestamps:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CompanySettings(_Timestamps, Base):
    __tablename__ = "company_settings"

    company_id: Mapped[UUID] = _uuid(primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    tax_rate_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    retirement_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    timezone: Mapped[str] = mapped_column(Text, nullable=False)
    biweekly_anchor: Mapped[date] = mapped_column(Date, nullable=False)
    require_dual_approval: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(Text)


class Employee(_Timestamps, Base):
    __tablename__ = "employees"

    id: Mapped[UUID] = _uuid(primary_key=True)
    company_id: Mapped[UUID] = _uuid()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    pay_type: Mapped[str] = mapped_column(Text, nullable=False)
    annual_salary_cents: Mapped[int | None] = mapped_column(BigInteger)
    hourly_rate_cents: Mapped[int | None] = mapped_column(BigInteger)
    schedule: Mapped[str] = mapped_column(Text, nullable=False)
    bank_routing_enc: Mapped[str | None] = mapped_column(Text)
    bank_account_enc: Mapped[str | None] = mapped_column(Text)
    account_last4: Mapped[str | None] = mapped_column(String(4))
    bank_updated_by: Mapped[str | None] = mapped_column(Text)
    bank_updated_at: Mapped[datetime | None] = _ts()
    bank_purged_at: Mapped[datetime | None] = _ts()
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    deactivated_at: Mapped[datetime | None] = _ts()
    created_by: Mapped[str] = mapped_column(Text, nullable=False)


class PayRun(_Timestamps, Base):
    __tablename__ = "pay_runs"

    id: Mapped[UUID] = _uuid(primary_key=True)
    company_id: Mapped[UUID] = _uuid()
    schedule: Mapped[str] = mapped_column(Text, nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    pay_date: Mapped[date] = mapped_column(Date, nullable=False)
    period_index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    periods_per_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    employee_count: Mapped[int] = mapped_column(Integer, nullable=False)
    tax_bps: Mapped[int | None] = mapped_column(Integer)
    retirement_bps: Mapped[int | None] = mapped_column(Integer)
    require_dual_approval: Mapped[bool | None] = mapped_column(Boolean)
    total_gross_cents: Mapped[int | None] = mapped_column(BigInteger)
    total_tax_cents: Mapped[int | None] = mapped_column(BigInteger)
    total_retirement_cents: Mapped[int | None] = mapped_column(BigInteger)
    total_net_cents: Mapped[int | None] = mapped_column(BigInteger)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    calculated_by: Mapped[str | None] = mapped_column(Text)
    calculated_by_name: Mapped[str | None] = mapped_column(Text)
    calculated_at: Mapped[datetime | None] = _ts()
    approved_by: Mapped[str | None] = mapped_column(Text)
    approved_by_name: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = _ts()
    processing_since: Mapped[datetime | None] = _ts()
    first_final_at: Mapped[datetime | None] = _ts()
    deleted_at: Mapped[datetime | None] = _ts()
    deleted_by: Mapped[str | None] = mapped_column(Text)


class Payslip(_Timestamps, Base):
    __tablename__ = "payslips"

    id: Mapped[UUID] = _uuid(primary_key=True)
    company_id: Mapped[UUID] = _uuid()
    pay_run_id: Mapped[UUID] = _uuid()
    employee_id: Mapped[UUID] = _uuid()
    employee_name: Mapped[str] = mapped_column(Text, nullable=False)
    pay_type: Mapped[str] = mapped_column(Text, nullable=False)
    annual_salary_cents: Mapped[int | None] = mapped_column(BigInteger)
    hourly_rate_cents: Mapped[int | None] = mapped_column(BigInteger)
    hours_hundredths: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    gross_cents: Mapped[int | None] = mapped_column(BigInteger)
    tax_cents: Mapped[int | None] = mapped_column(BigInteger)
    retirement_cents: Mapped[int | None] = mapped_column(BigInteger)
    net_cents: Mapped[int | None] = mapped_column(BigInteger)


class PayRunHours(_Timestamps, Base):
    __tablename__ = "pay_run_hours"

    id: Mapped[UUID] = _uuid(primary_key=True)
    company_id: Mapped[UUID] = _uuid()
    pay_run_id: Mapped[UUID] = _uuid()
    employee_id: Mapped[UUID] = _uuid()
    hours_hundredths: Mapped[int] = mapped_column(Integer, nullable=False)
    entered_by: Mapped[str] = mapped_column(Text, nullable=False)


class Payout(_Timestamps, Base):
    __tablename__ = "payouts"

    id: Mapped[UUID] = _uuid(primary_key=True)
    company_id: Mapped[UUID] = _uuid()
    pay_run_id: Mapped[UUID] = _uuid()
    payslip_id: Mapped[UUID] = _uuid()
    employee_id: Mapped[UUID] = _uuid()
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="USD")
    status: Mapped[str] = mapped_column(Text, nullable=False)
    no_transfer: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    current_attempt: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    final_at: Mapped[datetime | None] = _ts()


class PayoutAttempt(_Timestamps, Base):
    __tablename__ = "payout_attempts"

    id: Mapped[UUID] = _uuid(primary_key=True)
    company_id: Mapped[UUID] = _uuid()
    payout_id: Mapped[UUID] = _uuid()
    attempt: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="requested")
    failure_reason: Mapped[str | None] = mapped_column(Text)
    bank_reference: Mapped[str | None] = mapped_column(Text)
    bank_snapshot_enc: Mapped[str | None] = mapped_column(Text)
    account_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)
    requested_at: Mapped[datetime] = _ts(nullable=False)
    last_published_at: Mapped[datetime] = _ts(nullable=False)
    needs_operator_at: Mapped[datetime | None] = _ts()
    resolved_by: Mapped[str | None] = mapped_column(Text)  # last operator resolve (C73)
    publish_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    final_at: Mapped[datetime | None] = _ts()


class AuditEvent(_Timestamps, Base):
    __tablename__ = "audit_events"

    id: Mapped[UUID] = _uuid(primary_key=True)
    company_id: Mapped[UUID] = _uuid()
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[UUID] = _uuid()
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    request_id: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(Text)


class Outbox(_Timestamps, Base):
    __tablename__ = "outbox"

    id: Mapped[UUID] = _uuid(primary_key=True)
    company_id: Mapped[UUID] = _uuid()
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    event_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payout_id: Mapped[UUID | None] = _uuid(nullable=True)
    attempt: Mapped[int | None] = mapped_column(SmallInteger)
    sent_at: Mapped[datetime | None] = _ts()
    relay_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)


class IdempotencyKey(_Timestamps, Base):
    __tablename__ = "idempotency_keys"

    company_id: Mapped[UUID] = _uuid(primary_key=True)
    scope: Mapped[str] = mapped_column(Text, primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    response_body: Mapped[Any] = mapped_column(JSONB, nullable=False)
    expires_at: Mapped[datetime] = _ts(nullable=False)
