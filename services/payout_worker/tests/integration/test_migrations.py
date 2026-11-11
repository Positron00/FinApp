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
    for k, v in pg.env_for("payouts_migrator").items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(SERVICE_DIR)
    getattr(command, action)(Config(str(SERVICE_DIR / "alembic.ini")), rev)


async def _tables(pg: PgBootstrapped) -> set[str]:
    conn = await connect(pg, pg.admin_user)
    try:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'payouts'"
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
    assert asyncio.run(_tables(migrated)) == {
        "alembic_version",
        "disbursements",
        "processed_events",
    }


INSERT = (
    "INSERT INTO payouts.disbursements (id, payout_id, attempt, company_id, status, outcome, "
    "last_event_id) VALUES ($1, $2, $3, $4, $5, $6, $7)"
)


async def test_unique_attempt_and_done_needs_outcome(admin_db: Any) -> None:
    """Design §2.1: UNIQUE (payout_id, attempt); done ⇒ outcome; outcome enum."""
    pid, cid = uuid4(), uuid4()
    await admin_db.execute(INSERT, uuid4(), pid, 1, cid, "submitting", None, uuid4())
    with pytest.raises(asyncpg.UniqueViolationError):
        await admin_db.execute(INSERT, uuid4(), pid, 1, cid, "submitting", None, uuid4())
    with pytest.raises(asyncpg.CheckViolationError):
        await admin_db.execute(INSERT, uuid4(), pid, 2, cid, "done", None, uuid4())
    with pytest.raises(asyncpg.CheckViolationError):
        await admin_db.execute(INSERT, uuid4(), pid, 3, cid, "done", "failed", uuid4())
    with pytest.raises(asyncpg.CheckViolationError):
        await admin_db.execute(INSERT, uuid4(), pid, 0, cid, "submitting", None, uuid4())
