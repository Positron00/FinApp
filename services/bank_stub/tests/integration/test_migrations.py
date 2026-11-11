"""Migration 0001 (design §2.1, §2.2; PAY-606)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg  # type: ignore[import-untyped]
import pytest
from alembic import command
from alembic.config import Config
from pycommon.testing import PgBootstrapped

from tests.integration.conftest import connect

SERVICE_DIR = Path(__file__).resolve().parents[2]


def _alembic(pg: PgBootstrapped, monkeypatch: pytest.MonkeyPatch, action: str, rev: str) -> None:
    for k, v in pg.env_for("bank_stub_migrator").items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(SERVICE_DIR)
    getattr(command, action)(Config(str(SERVICE_DIR / "alembic.ini")), rev)


async def _tables(pg: PgBootstrapped) -> set[str]:
    conn = await connect(pg, pg.admin_user)
    try:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'bank_stub'"
        )
    finally:
        await conn.close()
    return {r["table_name"] for r in rows}


def test_upgrade_downgrade_upgrade(
    migrated: PgBootstrapped, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PAY-606: head → base → head."""
    _alembic(migrated, monkeypatch, "downgrade", "base")
    assert asyncio.run(_tables(migrated)) == {"alembic_version"}
    _alembic(migrated, monkeypatch, "upgrade", "head")
    assert asyncio.run(_tables(migrated)) == {"alembic_version", "transfers"}


INSERT = (
    "INSERT INTO bank_stub.transfers (id, idempotency_key, amount_cents, currency, "
    "account_last4, status, reason, bank_reference) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)"
)


@pytest.mark.parametrize(
    "row",
    [
        ("k:1", 100, "EUR", "1234", "settled", None, "BSR-0123456789ABCDEF"),  # USD only
        ("k:1", 0, "USD", "1234", "settled", None, "BSR-0123456789ABCDEF"),  # amount > 0
        ("k 1", 100, "USD", "1234", "settled", None, "BSR-0123456789ABCDEF"),  # key pattern
        ("k:1", 100, "USD", "12a4", "settled", None, "BSR-0123456789ABCDEF"),  # last4 digits
        ("k:1", 100, "USD", "1234", "settled", None, None),  # settled needs a reference
        ("k:1", 100, "USD", "1234", "rejected", None, None),  # rejected needs a reason
        ("k:1", 100, "USD", "1234", "rejected", "account_closed", "BSR-0123456789ABCDEF"),
        ("k:1", 100, "USD", "1234", "settled", None, "bsr-lowercase"),  # reference format
    ],
)
async def test_constraints(admin_db: Any, row: tuple[Any, ...]) -> None:
    """PAY-412: USD only, positive amount, key pattern, outcome shape, reference format."""
    with pytest.raises(asyncpg.CheckViolationError):
        await admin_db.execute(INSERT, uuid4(), *row)


async def test_unique_key(admin_db: Any) -> None:
    """PAY-412: one row per Idempotency-Key."""
    ok = ("k:1", 100, "USD", "1234", "rejected", "account_closed", None)
    await admin_db.execute(INSERT, uuid4(), *ok)
    with pytest.raises(asyncpg.UniqueViolationError):
        await admin_db.execute(INSERT, uuid4(), *ok)
