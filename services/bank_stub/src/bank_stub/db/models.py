"""SQLAlchemy Core table ``bank_stub.transfers`` (design §2.1; PAY-412, R13)."""

from __future__ import annotations

from typing import Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)

__all__ = ["SCHEMA", "metadata", "transfers"]

SCHEMA: Final = "bank_stub"

metadata = MetaData(schema=SCHEMA)

transfers = Table(
    "transfers",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("idempotency_key", Text, nullable=False),
    Column("amount_cents", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("account_last4", String(4), nullable=False),
    Column("status", Text, nullable=False),
    Column("reason", Text, nullable=True),
    Column("bank_reference", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("idempotency_key", name="transfers_idempotency_key_uq"),
    CheckConstraint(
        "(status = 'settled' AND reason IS NULL AND bank_reference IS NOT NULL) OR "
        "(status = 'rejected' AND reason IS NOT NULL AND bank_reference IS NULL)",
        name="transfers_outcome_shape",
    ),
)
