"""``python -m identity.bootstrap_owner`` against Postgres (identity LLD §3.8; PAY-100, PAY-609)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from identity.bootstrap_owner import EXIT_DB, EXIT_INVALID, EXIT_OK, EXIT_REFUSED, main
from pycommon.testing import JsonLogCapture, PgBootstrapped

from tests.helpers import COMPANY_ID
from tests.integration.conftest import HarnessFactory, free_port

OWNER = ["--email", "owner@payrun.example.com", "--company-id", str(COMPANY_ID)]
SEED_PW = "seed owner passphrase"


@pytest.fixture
def cli_env(
    migrated: PgBootstrapped, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> pytest.MonkeyPatch:
    """The bootstrap-owner container's env: migrator DB_* keys, COMPANY_ID, ENV=dev."""
    for k, v in migrated.env_for("identity_migrator").items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("COMPANY_ID", str(COMPANY_ID))
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("IDENTITY_ARGON2_TIME_COST", "1")
    monkeypatch.setenv("IDENTITY_ARGON2_MEMORY_KIB", "1024")
    monkeypatch.setenv("IDENTITY_ARGON2_PARALLELISM", "1")
    monkeypatch.delenv("SEED_OWNER_PASSWORD", raising=False)
    monkeypatch.chdir(tmp_path)
    return monkeypatch


async def run(*argv: str) -> int:
    return await asyncio.to_thread(main, list(argv))


async def counts(admin_db: Any) -> dict[str, int]:
    out = {}
    for t in ("users", "credentials", "invites", "email_outbox", "audit_log"):
        out[t] = await admin_db.fetchval(f"SELECT count(*) FROM identity.{t}")
    return out


async def test_invite_mode_then_refused_then_skipped(
    cli_env: pytest.MonkeyPatch, admin_db: Any, caplog_json: JsonLogCapture
) -> None:
    """PAY-100, R16: empty DB → 0 with a pending owner (all roles), an unsent invite and an
    outbox row, audited; a second run → 3 (refused); with --if-empty → 0 (skipped), no rows."""
    assert await run(*OWNER, "--name", "The Owner") == EXIT_OK
    owner = await admin_db.fetchrow("SELECT * FROM identity.users")
    assert owner["roles"] == ["admin", "approver", "payroll_admin"]
    assert (owner["status"], owner["name"], owner["created_by"]) == ("pending", "The Owner", None)
    inv = await admin_db.fetchrow("SELECT * FROM identity.invites")
    assert (inv["status"], inv["expires_at"], inv["token_hash"]) == ("pending", None, None)
    out = await admin_db.fetchrow("SELECT * FROM identity.email_outbox")
    assert (out["template"], out["invite_id"], out["status"]) == ("invite", inv["id"], "pending")
    audit = await admin_db.fetchrow("SELECT * FROM identity.audit_log")
    assert (audit["event"], audit["actor_user_id"]) == ("user.bootstrapped", None)
    assert audit["details"] == '{"mode": "invite"}'
    [done] = caplog_json.events("identity.bootstrap.completed")
    assert (done["mode"], done["user_id"]) == ("invite", str(owner["id"]))
    before = await counts(admin_db)

    assert await run(*OWNER) == EXIT_REFUSED
    assert len(caplog_json.events("identity.bootstrap.refused")) == 1
    assert await run(*OWNER, "--if-empty") == EXIT_OK
    assert len(caplog_json.events("identity.bootstrap.skipped")) == 1
    assert await counts(admin_db) == before
    assert "owner@payrun.example.com" not in caplog_json.text


async def test_concurrent_runs_serialise(cli_env: pytest.MonkeyPatch, admin_db: Any) -> None:
    """PAY-100: two concurrent runs on an empty DB → one 0 and one 3, one owner."""
    codes = await asyncio.gather(run(*OWNER), run(*OWNER))
    assert sorted(codes) == [EXIT_OK, EXIT_REFUSED]
    assert (await counts(admin_db))["users"] == 1


async def test_password_mode_creates_an_active_owner_who_can_log_in(
    cli_env: pytest.MonkeyPatch,
    admin_db: Any,
    harness: HarnessFactory,
    caplog_json: JsonLogCapture,
) -> None:
    """PAY-100, PAY-609, R16: --if-empty --password-env SEED_OWNER_PASSWORD in dev → an active
    owner with that password, no invite and no email; the password is never logged."""
    cli_env.setenv("SEED_OWNER_PASSWORD", SEED_PW)
    assert await run(*OWNER, "--if-empty", "--password-env", "SEED_OWNER_PASSWORD") == EXIT_OK
    c = await counts(admin_db)
    assert c == {"users": 1, "credentials": 1, "invites": 0, "email_outbox": 0, "audit_log": 1}
    owner = await admin_db.fetchrow("SELECT * FROM identity.users")
    assert owner["status"] == "active"
    assert owner["activated_at"] is not None
    assert await admin_db.fetchval("SELECT details->>'mode' FROM identity.audit_log") == "password"
    assert SEED_PW not in caplog_json.text
    async with harness() as h:
        r = await h.login("owner@payrun.example.com", SEED_PW)
    assert r.status_code == 200
    assert r.json()["roles"] == ["admin", "approver", "payroll_admin"]


async def test_password_env_outside_dev_is_2(cli_env: pytest.MonkeyPatch, admin_db: Any) -> None:
    """PAY-100: --password-env with ENV=uat exits 2 and writes nothing."""
    cli_env.setenv("ENV", "uat")
    cli_env.setenv("SEED_OWNER_PASSWORD", SEED_PW)
    assert await run(*OWNER, "--password-env", "SEED_OWNER_PASSWORD") == EXIT_INVALID
    assert (await counts(admin_db))["users"] == 0


async def test_db_down_is_4(cli_env: pytest.MonkeyPatch, caplog_json: JsonLogCapture) -> None:
    """PAY-100: an unreachable database exits 4 (identity.bootstrap.failed db_unavailable)."""
    cli_env.setenv("DB_PORT", str(free_port()))
    assert await run(*OWNER) == EXIT_DB
    [failed] = caplog_json.events("identity.bootstrap.failed")
    assert failed["reason"] == "db_unavailable"


async def test_migrations_not_at_head_is_4(cli_env: pytest.MonkeyPatch, admin_db: Any) -> None:
    """PAY-100: migrations not at head exit 4 and write nothing."""
    await admin_db.execute("UPDATE identity.alembic_version SET version_num = 'older'")
    try:
        assert await run(*OWNER) == EXIT_DB
    finally:
        await admin_db.execute("UPDATE identity.alembic_version SET version_num = '0001_initial'")
    assert (await counts(admin_db))["users"] == 0
