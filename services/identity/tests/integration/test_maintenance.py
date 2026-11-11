"""Identity maintenance purges (identity LLD §6.10, §7.2; PAY-608, PAY-NFR-08, C44, C50, C63)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from identity import maintenance
from identity.maintenance import LOCK_NAME, TASK_NAMES, build_runner, identity_tasks
from pycommon.db import advisory_lock_key, new_id
from pycommon.maintenance import MaintenanceRunner
from pycommon.testing import FakeClock, JsonLogCapture

from tests.helpers import COMPANY_ID, passphrase
from tests.integration.conftest import Harness, HarnessFactory, wait_until

PW = passphrase()
TOKEN_HASH = bytes(32)


def runner(h: Harness, clock: Any = None) -> MaintenanceRunner:
    ctx = h.app.state.ctx
    return build_runner(h.rt.settings, ctx.engine, ctx.sessions, clock)


async def add_invite(
    db: Any,
    user_id: UUID,
    *,
    status: str,
    expires_days_ago: float | None,
    updated_days_ago: float = 0,
    hashed: bool = True,
) -> UUID:
    invite_id = new_id()
    await db.execute(
        "INSERT INTO identity.invites (id, company_id, user_id, status, token_hash, expires_at, "
        "updated_at) VALUES ($1, $2, $3, $4, $5, "
        "CASE WHEN $6::float8 IS NULL THEN NULL ELSE now() - $6::float8 * interval '1 day' END, "
        "now() - $7::float8 * interval '1 day')",
        invite_id,
        COMPANY_ID,
        user_id,
        status,
        new_id().bytes * 2 if hashed and expires_days_ago is not None else None,
        expires_days_ago,
        updated_days_ago,
    )
    return invite_id


async def add_email(
    db: Any, invite_id: UUID, *, status: str, finished_days_ago: float | None
) -> UUID:
    email_id = new_id()
    await db.execute(
        "INSERT INTO identity.email_outbox (id, company_id, template, recipient, invite_id, "
        "status, finished_at) VALUES ($1, $2, 'invite', 'x@example.com', $3, $4, "
        "CASE WHEN $5::float8 IS NULL THEN NULL ELSE now() - $5::float8 * interval '1 day' END)",
        email_id,
        COMPANY_ID,
        invite_id,
        status,
        finished_days_ago,
    )
    return email_id


async def ids(db: Any, table: str) -> set[UUID]:
    return {r["id"] for r in await db.fetch(f"SELECT id FROM identity.{table}")}


async def test_one_tick_purges_rows_past_each_window(
    harness: HarnessFactory, admin_db: Any, caplog_json: JsonLogCapture
) -> None:
    """PAY-608: after one tick, expired token hashes are nulled 7 days after expiry (a pending
    user's latest invite is kept so it can be resent), accepted and revoked invites and resets
    7 days past expiry (or revocation, when never sent) are deleted, sent, failed and cancelled
    emails 7 days after they finished, and audit rows past 400 days; younger rows remain and
    each task logs maintenance.task.completed (C44)."""
    async with harness() as h:
        pending = await h.create_user("p@example.com", status="pending")
        active = await h.create_user("a@example.com", password=PW)
        # Invites
        keep_hash = await add_invite(admin_db, pending, status="pending", expires_days_ago=6)
        recent_accepted = await add_invite(admin_db, active, status="accepted", expires_days_ago=1)
        await admin_db.execute(
            "UPDATE identity.invites SET status = 'revoked' WHERE id = $1", keep_hash
        )
        latest = await add_invite(admin_db, pending, status="pending", expires_days_ago=8)
        old_revoked = await add_invite(admin_db, pending, status="revoked", expires_days_ago=8)
        never_sent = await add_invite(
            admin_db, pending, status="revoked", expires_days_ago=None, updated_days_ago=8
        )
        recent_never_sent = await add_invite(
            admin_db, pending, status="revoked", expires_days_ago=None, updated_days_ago=6
        )
        # Emails (on an invite that stays)
        old_sent = await add_email(admin_db, latest, status="sent", finished_days_ago=8)
        old_failed = await add_email(admin_db, latest, status="failed", finished_days_ago=8)
        old_cancelled = await add_email(admin_db, latest, status="cancelled", finished_days_ago=8)
        new_sent = await add_email(admin_db, latest, status="sent", finished_days_ago=6)
        still_pending = await add_email(admin_db, latest, status="pending", finished_days_ago=None)
        # Password resets
        old_reset, new_reset, never_sent_reset = new_id(), new_id(), new_id()
        for reset_id, expires, updated in (
            (old_reset, 8.0, 0.0),
            (new_reset, 6.0, 0.0),
            (never_sent_reset, None, 8.0),
        ):
            await admin_db.execute(
                "INSERT INTO identity.password_resets (id, company_id, user_id, status, "
                "expires_at, updated_at) VALUES ($1, $2, $3, 'revoked', "
                "CASE WHEN $4::float8 IS NULL THEN NULL "
                "ELSE now() - $4::float8 * interval '1 day' END, "
                "now() - $5::float8 * interval '1 day')",
                reset_id,
                COMPANY_ID,
                active,
                expires,
                updated,
            )
        # Audit rows
        old_audit, young_audit = new_id(), new_id()
        for audit_id, days in ((old_audit, 401), (young_audit, 399)):
            await admin_db.execute(
                "INSERT INTO identity.audit_log (id, company_id, event, at) "
                "VALUES ($1, $2, 'auth.signin.failed', now() - make_interval(days => $3))",
                audit_id,
                COMPANY_ID,
                days,
            )

        assert await runner(h).tick() is True

    invites = {r["id"]: r for r in await admin_db.fetch("SELECT * FROM identity.invites")}
    assert set(invites) == {keep_hash, recent_accepted, latest, recent_never_sent}
    assert invites[latest]["token_hash"] is None
    assert invites[latest]["status"] == "pending"
    assert invites[keep_hash]["token_hash"] is not None
    assert invites[recent_accepted]["token_hash"] is not None
    assert old_revoked not in invites
    assert never_sent not in invites
    assert await ids(admin_db, "email_outbox") == {new_sent, still_pending}
    assert {old_sent, old_failed, old_cancelled}.isdisjoint(await ids(admin_db, "email_outbox"))
    assert await ids(admin_db, "password_resets") == {new_reset}
    assert old_reset not in await ids(admin_db, "password_resets")
    assert never_sent_reset not in await ids(admin_db, "password_resets")
    assert await ids(admin_db, "audit_log") == {young_audit}
    done = {e["task"]: e["affected"] for e in caplog_json.events("maintenance.task.completed")}
    assert done == {
        # latest, and old_revoked before purge_invites deletes it
        "null_invite_hashes": 2,
        "purge_invites": 2,
        "purge_password_resets": 2,
        "purge_emails": 3,
        "purge_audit": 1,
    }
    assert tuple(done) == TASK_NAMES


async def test_cutoffs_follow_the_clock_and_settings(
    harness: HarnessFactory, admin_db: Any
) -> None:
    """PAY-608: cutoffs come from the Clock and RETENTION_* keys: an email sent just now is
    purged once the clock is 7 days on, and RETENTION_OUTBOX_DAYS moves the window; the audit
    purge never deletes rows younger than 400 days whatever the clock says (clamped, C63)."""
    async with harness(retention_outbox_days=2) as h:
        user = await h.create_user("p@example.com", status="pending")
        invite_id = await add_invite(admin_db, user, status="pending", expires_days_ago=None)
        sent = await add_email(admin_db, invite_id, status="sent", finished_days_ago=0)
        await admin_db.execute(
            "INSERT INTO identity.audit_log (id, company_id, event) "
            "VALUES ($1, $2, 'auth.signin.failed')",
            new_id(),
            COMPANY_ID,
        )
        now = datetime.now(UTC)
        tasks = {t.name: t for t in identity_tasks(h.rt.settings, FakeClock(now))}
        ctx = h.app.state.ctx
        async with ctx.sessions() as session, session.begin():
            assert await tasks["purge_emails"].fn(session) == 0
        later = FakeClock(now + timedelta(days=2, minutes=1))
        tasks = {t.name: t for t in identity_tasks(h.rt.settings, later)}
        async with ctx.sessions() as session, session.begin():
            assert await tasks["purge_emails"].fn(session) == 1
        far = FakeClock(now + timedelta(days=4000))
        tasks = {t.name: t for t in identity_tasks(h.rt.settings, far)}
        async with ctx.sessions() as session, session.begin():
            assert await tasks["purge_audit"].fn(session) == 0
    assert sent not in await ids(admin_db, "email_outbox")
    assert await admin_db.fetchval("SELECT count(*) FROM identity.audit_log") == 1


async def test_batches_cover_more_than_one_batch(
    harness: HarnessFactory, admin_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PAY-608: each task repeats its batch until a short batch, so it finishes in one tick."""
    monkeypatch.setattr(maintenance, "BATCH", 2)
    async with harness() as h:
        user = await h.create_user("p@example.com", status="pending")
        invite_id = await add_invite(admin_db, user, status="pending", expires_days_ago=None)
        for _ in range(5):
            await add_email(admin_db, invite_id, status="sent", finished_days_ago=8)
        tasks = {t.name: t for t in identity_tasks(h.rt.settings)}
        async with h.app.state.ctx.sessions() as session, session.begin():
            assert await tasks["purge_emails"].fn(session) == 5
    assert await ids(admin_db, "email_outbox") == set()


async def test_only_one_replica_runs_a_tick(harness: HarnessFactory, admin_db: Any) -> None:
    """PAY-608: the tick runs under pg_try_advisory_lock('identity:maintenance'): while another
    session holds it, a replica skips the tick (deletes nothing); once released, it runs."""
    async with harness() as h:
        user = await h.create_user("p@example.com", status="pending")
        invite_id = await add_invite(admin_db, user, status="pending", expires_days_ago=None)
        old = await add_email(admin_db, invite_id, status="sent", finished_days_ago=8)
        key = advisory_lock_key(LOCK_NAME)
        assert await admin_db.fetchval("SELECT pg_try_advisory_lock($1)", key)
        try:
            assert await runner(h).tick() is False
            assert old in await ids(admin_db, "email_outbox")
        finally:
            await admin_db.execute("SELECT pg_advisory_unlock($1)", key)
        assert await runner(h).tick() is True
    assert old not in await ids(admin_db, "email_outbox")


async def test_the_app_runs_maintenance_in_the_background(
    harness: HarnessFactory, admin_db: Any, caplog_json: JsonLogCapture
) -> None:
    """PAY-608: the identity app starts the runner as a background task (with a FakeClock, so
    its first-tick delay and interval pass at once); a tick purges an old email."""
    async with harness(run_maintenance=True, clock=FakeClock(datetime.now(UTC))) as h:
        user = await h.create_user("p@example.com", status="pending")
        invite_id = await add_invite(admin_db, user, status="pending", expires_days_ago=None)
        old = await add_email(admin_db, invite_id, status="sent", finished_days_ago=8)

        async def purged() -> bool:
            return old not in await ids(admin_db, "email_outbox")

        await wait_until(purged)
    assert any(e["task"] == "purge_audit" for e in caplog_json.events("maintenance.task.completed"))
