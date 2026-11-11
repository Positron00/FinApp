"""Migrations up, down and up; the settings seed (design §2.11; PAY-200, PAY-606, C73)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from pycommon.testing import PgBootstrapped, find_repo_root

from tests.helpers import COMPANY_ID
from tests.integration.conftest import TABLES, admin_connect

pytestmark = pytest.mark.integration

SERVICE_DIR = find_repo_root() / "services" / "payroll"
HEAD = "0003_c73_four_eyes"


@pytest.fixture
def as_migrator(migrated: PgBootstrapped, monkeypatch: pytest.MonkeyPatch) -> Config:
    for k, v in migrated.env_for("payroll_migrator").items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("COMPANY_ID", str(COMPANY_ID))
    monkeypatch.chdir(SERVICE_DIR)
    return Config(str(SERVICE_DIR / "alembic.ini"))


async def _tables(conn: Any) -> set[str]:
    rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'payroll'")
    return {r["tablename"] for r in rows}


async def _version(conn: Any) -> str | None:
    value: str | None = await conn.fetchval("SELECT version_num FROM payroll.alembic_version")
    return value


async def _audit_privileges(conn: Any) -> set[str]:
    rows = await conn.fetch(
        "SELECT privilege_type FROM information_schema.role_table_grants "
        "WHERE table_schema = 'payroll' AND table_name = 'audit_events' "
        "AND grantee = 'payroll_app'"
    )
    return {r["privilege_type"] for r in rows}


async def _has_resolved_by(conn: Any) -> bool:
    return bool(
        await conn.fetchval(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'payroll' "
            "AND table_name = 'payout_attempts' AND column_name = 'resolved_by'"
        )
    )


async def test_upgrade_downgrade_upgrade(
    migrated: PgBootstrapped, as_migrator: Config, db: Any
) -> None:
    """PAY-606, C73: head → 0002 (resolved_by gone, audit DML re-granted) → 0001 (seed removed)
    → base (tables gone) → head again (audit append-only, resolved_by back)."""
    conn = await admin_connect(migrated)
    try:
        assert await _tables(conn) >= set(TABLES)
        assert await _version(conn) == HEAD
        assert await _audit_privileges(conn) == {"SELECT", "INSERT"}
        assert await _has_resolved_by(conn)
        await conn.execute("TRUNCATE payroll.company_settings CASCADE")
        command.upgrade(as_migrator, "head")  # re-running is a no-op
        command.downgrade(as_migrator, "-1")
        assert await _version(conn) == "0002_seed_company_settings"
        assert await _audit_privileges(conn) == {"SELECT", "INSERT", "UPDATE", "DELETE"}
        assert not await _has_resolved_by(conn)
        command.downgrade(as_migrator, "-1")
        assert await _version(conn) == "0001_initial_schema"
        command.downgrade(as_migrator, "base")
        assert await _tables(conn) == {"alembic_version"}
        command.upgrade(as_migrator, "head")
        assert await _tables(conn) >= set(TABLES)
        assert await _version(conn) == HEAD
        assert await _audit_privileges(conn) == {"SELECT", "INSERT"}
        assert await _has_resolved_by(conn)
    finally:
        await conn.close()


async def test_seed_defaults_and_idempotent_rerun(
    migrated: PgBootstrapped, as_migrator: Config, db: Any
) -> None:
    """PAY-200: 0002 seeds tax 0, retirement 0, the default timezone, anchor and dual flag;
    re-running keeps an existing (edited) row."""
    await db.execute("TRUNCATE payroll.company_settings CASCADE")
    command.downgrade(as_migrator, "0001_initial_schema")
    command.upgrade(as_migrator, "head")
    row = await db.fetchrow(
        "SELECT * FROM payroll.company_settings WHERE company_id = $1", COMPANY_ID
    )
    assert row["tax_rate_bps"] == 0
    assert row["retirement_bps"] == 0
    assert row["timezone"] == "America/New_York"
    assert str(row["biweekly_anchor"]) == "2026-01-05"
    assert row["require_dual_approval"] is True
    assert row["name"] == "PayRun Demo Company"
    command.downgrade(as_migrator, "0001_initial_schema")  # deletes the seed (no employees)
    assert await db.fetchval("SELECT count(*) FROM payroll.company_settings") == 0
    await db.execute(
        "INSERT INTO payroll.company_settings (company_id, name, tax_rate_bps, retirement_bps) "
        "VALUES ($1, 'Edited', 2000, 0)",
        COMPANY_ID,
    )
    command.upgrade(as_migrator, "head")
    assert (
        await db.fetchval(
            "SELECT name FROM payroll.company_settings WHERE company_id = $1", COMPANY_ID
        )
        == "Edited"
    )


def test_missing_company_id_fails(
    as_migrator: Config, monkeypatch: pytest.MonkeyPatch, migrated: PgBootstrapped
) -> None:
    """PAY-200: without COMPANY_ID the seed migration fails loudly (KeyError)."""
    command.downgrade(as_migrator, "0001_initial_schema")
    monkeypatch.delenv("COMPANY_ID")
    try:
        with pytest.raises(KeyError, match="COMPANY_ID"):
            command.upgrade(as_migrator, "head")
    finally:
        monkeypatch.setenv("COMPANY_ID", str(COMPANY_ID))
        command.upgrade(as_migrator, "head")


def test_alembic_ini_resolves_the_scripts() -> None:
    """PAY-606: script_location is %(here)s/migrations, valid in the repo and the image."""
    cfg = Config(str(SERVICE_DIR / "alembic.ini"))
    assert Path(cfg.get_main_option("script_location") or "") == SERVICE_DIR / "migrations"
    assert os.path.isfile(SERVICE_DIR / "migrations" / "env.py")
