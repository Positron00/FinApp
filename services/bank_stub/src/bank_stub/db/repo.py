"""``TransferRepo``: replay lookup and the race-safe insert (design §3.1 steps 4 and 7; PAY-412).

``insert_or_get`` runs ``INSERT … ON CONFLICT (idempotency_key) DO NOTHING RETURNING *`` and, when
no row comes back (a concurrent request inserted the key first), a fresh ``SELECT`` in the same
READ COMMITTED transaction, which sees the committed row. One row per key, whatever the race.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bank_stub.db.models import transfers

__all__ = ["NewTransfer", "TransferRepo", "TransferRepoPort", "TransferRow"]


@dataclass(frozen=True)
class TransferRow:
    id: UUID
    idempotency_key: str
    status: str
    reason: str | None
    bank_reference: str | None
    created_at: datetime | None = None

    @classmethod
    def from_mapping(cls, m: Any) -> TransferRow:
        return cls(
            id=m["id"],
            idempotency_key=m["idempotency_key"],
            status=m["status"],
            reason=m["reason"],
            bank_reference=m["bank_reference"],
            created_at=m["created_at"],
        )


@dataclass(frozen=True)
class NewTransfer:
    id: UUID
    idempotency_key: str
    amount_cents: int
    currency: str
    account_last4: str
    status: str
    reason: str | None
    bank_reference: str | None

    def __repr__(self) -> str:  # no amount or account digits in reprs (PAY-NFR-07)
        return f"NewTransfer(id={self.id}, key={self.idempotency_key!r}, status={self.status!r})"


class TransferRepoPort(Protocol):
    async def get_by_key(self, key: str) -> TransferRow | None: ...

    async def insert_or_get(self, new: NewTransfer) -> tuple[TransferRow, bool]: ...


class TransferRepo:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get_by_key(self, key: str) -> TransferRow | None:
        async with self._sessions() as session:
            row = (
                (await session.execute(select(transfers).where(transfers.c.idempotency_key == key)))
                .mappings()
                .first()
            )
        return TransferRow.from_mapping(row) if row is not None else None

    async def insert_or_get(self, new: NewTransfer) -> tuple[TransferRow, bool]:
        stmt = (
            insert(transfers)
            .values(
                id=new.id,
                idempotency_key=new.idempotency_key,
                amount_cents=new.amount_cents,
                currency=new.currency,
                account_last4=new.account_last4,
                status=new.status,
                reason=new.reason,
                bank_reference=new.bank_reference,
            )
            .on_conflict_do_nothing(index_elements=[transfers.c.idempotency_key])
            .returning(*transfers.c)
        )
        async with self._sessions() as session, session.begin():
            inserted = (await session.execute(stmt)).mappings().first()
            if inserted is not None:
                return TransferRow.from_mapping(inserted), True
            existing = (
                (
                    await session.execute(
                        select(transfers).where(transfers.c.idempotency_key == new.idempotency_key)
                    )
                )
                .mappings()
                .one()
            )
        return TransferRow.from_mapping(existing), False
