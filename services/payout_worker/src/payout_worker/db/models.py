"""SQLAlchemy Core tables ``payouts.disbursements`` and ``payouts.processed_events`` (design §2.1;
PAY-403, R13, C22). No account numbers, names or amounts."""

from __future__ import annotations

from typing import Final

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)

__all__ = ["OUTCOMES", "SCHEMA", "disbursements", "metadata", "processed_events"]

SCHEMA: Final = "payouts"
OUTCOMES: Final = ("settled", "rejected", "indeterminate", "poison", "attempt_not_current")

metadata = MetaData(schema=SCHEMA)

disbursements = Table(
    "disbursements",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("payout_id", Uuid, nullable=False),
    Column("attempt", SmallInteger, nullable=False),
    Column("company_id", Uuid, nullable=False),
    Column("status", Text, nullable=False),
    Column("outcome", Text, nullable=True),
    Column("reason", Text, nullable=True),
    Column("bank_reference", Text, nullable=True),
    Column("last_event_id", Uuid, nullable=False),
    Column("deliveries", Integer, nullable=False, server_default="1"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("payout_id", "attempt", name="disbursements_payout_attempt_uq"),
    CheckConstraint(
        "status <> 'done' OR outcome IS NOT NULL", name="disbursements_done_has_outcome"
    ),
)

processed_events = Table(
    "processed_events",
    metadata,
    Column("event_id", Uuid, primary_key=True),
    Column("stream", Text, nullable=False),
    Column("payout_id", Uuid, nullable=False),
    Column("attempt", SmallInteger, nullable=False),
    Column("company_id", Uuid, nullable=False),
    Column("outcome", Text, nullable=False),
    Column("processed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("processed_events_processed_at_idx", "processed_at"),
)
