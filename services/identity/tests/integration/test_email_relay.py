"""The email relay to SMTP (identity LLD §6.9, §7.2; PAY-110, R17, R25, C12, C31)."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from identity.email.relay import EmailRelay
from identity.email.smtp import SmtpSender
from pycommon.db import new_id
from pycommon.testing import JsonLogCapture, OtelMemory

from tests.helpers import COMPANY_ID
from tests.integration.conftest import (
    FakeSmtp,
    Harness,
    HarnessFactory,
    text_of,
    token_in,
    wait_until,
)

PW = "correct horse battery staple"


async def invite(h: Harness, admin: UUID, email: str = "bea@example.com") -> Any:
    r = await h.client.post(
        "/v1/users/invite",
        json={"email": email, "name": "Bea", "roles": ["approver"]},
        headers=h.user_headers(admin, ("admin",)),
    )
    assert r.status_code == 201
    return r.json()


async def outbox(admin_db: Any) -> list[Any]:
    rows: list[Any] = await admin_db.fetch("SELECT * FROM identity.email_outbox ORDER BY id")
    return rows


async def test_invite_is_delivered_over_smtp_and_its_link_works(
    harness: HarnessFactory, smtp_server: FakeSmtp, admin_db: Any
) -> None:
    """PAY-110, PAY-101, PAY-103, C12: the running relay sends one message whose
    http://127.0.0.1:4100/accept#token= link accepts the invite; the row is 'sent'."""
    smtp_server.messages.clear()
    async with harness(sender="smtp", run_relay=True, identity_email_poll_interval_s=0.05) as h:
        admin = await h.create_user("admin@example.com", password=PW)
        await invite(h, admin)
        await wait_until(lambda: len(smtp_server.messages) == 1)
        msg = smtp_server.messages[0]
        assert msg["To"] == "bea@example.com"
        assert msg["Subject"] == "You're invited to PayRun"
        assert msg["Auto-Submitted"] == "auto-generated"
        assert "http://127.0.0.1:4100/accept#token=" in text_of(msg)
        assert (await h.accept(token_in(msg), "a brand new passphrase")).status_code == 200

        async def sent() -> bool:
            return [r["status"] for r in await outbox(admin_db)] == ["sent"]

        await wait_until(sent)
    [row] = await outbox(admin_db)
    assert row["attempts"] == 1
    assert row["finished_at"] is not None
    assert row["last_error"] is None


async def test_smtp_down_then_recovered(
    harness: HarnessFactory, smtp_server: FakeSmtp, admin_db: Any, caplog_json: JsonLogCapture
) -> None:
    """PAY-110: with SMTP down the invite is still 201 and the row stays pending with growing
    attempts and a class-only last_error; after recovery it is sent."""
    smtp_server.messages.clear()
    async with harness(sender="smtp") as h:
        admin = await h.create_user("admin@example.com", password=PW)
        await invite(h, admin)
        smtp_server.stop()
        try:
            for n in (1, 2):
                await h.rt.relay.run_once()
                [row] = await outbox(admin_db)
                assert (row["status"], row["attempts"]) == ("pending", n)
                assert row["last_error"] in {"SMTPConnectError", "ConnectionRefusedError"}
                await admin_db.execute("UPDATE identity.email_outbox SET next_attempt_at = now()")
        finally:
            smtp_server.start()
        await h.rt.relay.run_once()
    [row] = await outbox(admin_db)
    assert (row["status"], row["attempts"]) == ("sent", 3)
    assert len(smtp_server.messages) == 1
    failed = caplog_json.events("email.send_failed")
    assert len(failed) == 2
    assert failed[0]["next_attempt_in_s"] >= 27


async def test_exhausted_email_is_failed_and_counted(
    harness: HarnessFactory, smtp_server: FakeSmtp, admin_db: Any, otel_memory: OtelMemory
) -> None:
    """PAY-110, R25: the EMAIL_MAX_ATTEMPTS-th failure marks the row failed and increments
    identity_emails_failed_total (exported as 0 from start)."""
    async with harness(sender="smtp", email_max_attempts=2, breaker_failures=100) as h:
        assert [v for _, v in otel_memory.metric_points("identity_emails_failed")] == [0]
        admin = await h.create_user("admin@example.com", password=PW)
        await invite(h, admin)
        smtp_server.stop()
        try:
            await h.rt.relay.run_once()
            await admin_db.execute("UPDATE identity.email_outbox SET next_attempt_at = now()")
            await h.rt.relay.run_once()
        finally:
            smtp_server.start()
    [row] = await outbox(admin_db)
    assert (row["status"], row["attempts"]) == ("failed", 2)
    assert row["finished_at"] is not None
    assert [v for _, v in otel_memory.metric_points("identity_emails_failed")] == [1]
    outcomes = {
        a["outcome"]: v
        for a, v in otel_memory.metric_points("external_requests")
        if a.get("provider") == "smtp"
    }
    assert outcomes == {"connect_error": 2}


async def test_open_breaker_does_not_count_an_attempt(
    harness: HarnessFactory, smtp_server: FakeSmtp, admin_db: Any
) -> None:
    """PAY-110: when the breaker is open the claim's attempt is given back and the row waits
    BREAKER_RESET_S."""
    async with harness(sender="smtp", breaker_failures=1, breaker_reset_s=300) as h:
        admin = await h.create_user("admin@example.com", password=PW)
        await invite(h, admin)
        await invite(h, admin, email="cy@example.com")
        smtp_server.stop()
        try:
            await h.rt.relay.run_once()
        finally:
            smtp_server.start()
    first, second = await outbox(admin_db)
    assert (first["attempts"], first["status"]) == (1, "pending")
    assert (second["attempts"], second["last_error"]) == (0, "breaker_open")
    wait = await admin_db.fetchval(
        "SELECT extract(epoch FROM next_attempt_at - now()) FROM identity.email_outbox "
        "WHERE id = $1",
        second["id"],
    )
    assert 250 < wait <= 300


async def test_two_relays_never_send_a_row_twice(
    harness: HarnessFactory, smtp_server: FakeSmtp, admin_db: Any
) -> None:
    """PAY-110: SKIP LOCKED claims, so two relays on one table send each row once."""
    smtp_server.messages.clear()
    async with harness(sender="smtp", identity_email_batch=3) as h:
        admin = await h.create_user("admin@example.com", password=PW)
        for i in range(8):
            await invite(h, admin, email=f"u{i}@example.com")
        other = EmailRelay(h.rt.sessions, h.rt.settings, SmtpSender(h.rt.settings), h.rt.metrics)
        for _ in range(4):
            await asyncio.gather(h.rt.relay.run_once(), other.run_once())
    rows = await outbox(admin_db)
    assert [r["status"] for r in rows] == ["sent"] * 8
    assert [r["attempts"] for r in rows] == [1] * 8
    assert sorted(m["To"] for m in smtp_server.messages) == sorted(
        f"u{i}@example.com" for i in range(8)
    )


async def test_not_issuable_rows_are_cancelled(
    harness: HarnessFactory, admin_db: Any, caplog_json: JsonLogCapture
) -> None:
    """PAY-110, R17: a revoked invite (or a reset whose user isn't active) gets no token: the
    row is cancelled and nothing is sent."""
    async with harness() as h:
        admin = await h.create_user("admin@example.com", password=PW)
        await invite(h, admin)
        await admin_db.execute("UPDATE identity.invites SET status = 'revoked'")
        gone = await h.create_user("gone@example.com", status="inactive")
        reset_id = new_id()
        await admin_db.execute(
            "INSERT INTO identity.password_resets (id, company_id, user_id) VALUES ($1, $2, $3)",
            reset_id,
            COMPANY_ID,
            gone,
        )
        await admin_db.execute(
            "INSERT INTO identity.email_outbox (id, company_id, template, recipient, "
            "password_reset_id) VALUES ($1, $2, 'password_reset', 'gone@example.com', $3)",
            new_id(),
            COMPANY_ID,
            reset_id,
        )
        await h.rt.relay.run_once()
        assert h.sender is not None
        assert h.sender.messages == []
    assert [r["status"] for r in await outbox(admin_db)] == ["cancelled", "cancelled"]
    reasons = sorted(e["reason"] for e in caplog_json.events("email.cancelled"))
    assert reasons == ["invite_not_pending", "reset_not_pending"]


async def test_reset_email_gets_a_token_at_send(harness: HarnessFactory, admin_db: Any) -> None:
    """PAY-105, R17, C31: the relay issues a reset token at send; expires_at = send + 1 h."""
    async with harness() as h:
        uid = await h.create_user("ann@example.com", password=PW)
        reset_id = new_id()
        await admin_db.execute(
            "INSERT INTO identity.password_resets (id, company_id, user_id) VALUES ($1, $2, $3)",
            reset_id,
            COMPANY_ID,
            uid,
        )
        await admin_db.execute(
            "INSERT INTO identity.email_outbox (id, company_id, template, recipient, "
            "password_reset_id) VALUES ($1, $2, 'password_reset', 'ann@example.com', $3)",
            new_id(),
            COMPANY_ID,
            reset_id,
        )
        await h.rt.relay.run_once()
        assert h.sender is not None
        [msg] = h.sender.messages
    assert "http://127.0.0.1:4100/reset#token=" in text_of(msg)
    row = await admin_db.fetchrow("SELECT * FROM identity.password_resets")
    assert (row["expires_at"] - row["token_issued_at"]).total_seconds() == 3600


async def test_no_column_holds_the_token_or_link(harness: HarnessFactory, admin_db: Any) -> None:
    """PAY-110, PAY-NFR-07: the outbox, invites and audit rows never hold the raw token or link."""
    async with harness() as h:
        admin = await h.create_user("admin@example.com", password=PW)
        await invite(h, admin)
        await h.rt.relay.run_once()
        assert h.sender is not None
        token = token_in(h.sender.messages[0])
    for table in ("email_outbox", "invites", "audit_log", "users"):
        dump = " ".join(
            str(dict(r)) for r in await admin_db.fetch(f"SELECT * FROM identity.{table}")
        )
        assert token not in dump
        assert "#token=" not in dump
