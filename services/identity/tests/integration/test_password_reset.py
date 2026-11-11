"""Password reset request and confirm (identity LLD §6.6, §7.2; PAY-105, PAY-107, R17-R19, C31)."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

from identity.domain.ip import ip_hash
from pycommon.testing import JsonLogCapture, OtelMemory

from tests.helpers import AUDIT_KEY, passphrase
from tests.integration.conftest import CLIENT_IP, Harness, HarnessFactory, token_in

PW = passphrase()
NEW_PW = passphrase()


async def send_and_get_token(h: Harness) -> str:
    assert h.sender is not None
    await h.rt.relay.run_once()
    return token_in(h.sender.messages[-1])


async def requested_reset_token(h: Harness, email: str) -> str:
    r = await h.reset_request(email)
    assert r.status_code == 202
    return await send_and_get_token(h)


async def test_request_is_202_for_everyone_but_queues_only_for_active_users(
    harness: HarnessFactory, admin_db: Any, caplog_json: JsonLogCapture
) -> None:
    """PAY-105, PAY-NFR-08: 202 with an empty body for an active, unknown, pending or inactive
    email; only the active user gets a reset, an outbox row and an auth.password_reset.requested
    audit row (with ip_hash); the reset's expires_at stays NULL until the relay sends (C31)."""
    async with harness() as h:
        ann = await h.create_user("ann@example.com", password=PW)
        await h.create_user("gone@example.com", password=PW, status="inactive")
        await h.create_user("pend@example.com", status="pending")
        for email in ("ANN@example.com", "nobody@example.com", "gone@example.com", "pend@x.com"):
            r = await h.reset_request(email)
            assert r.status_code == 202
            assert r.content == b""
    [reset] = await admin_db.fetch("SELECT * FROM identity.password_resets")
    assert (reset["user_id"], reset["status"]) == (ann, "pending")
    assert (reset["token_hash"], reset["expires_at"]) == (None, None)
    [out] = await admin_db.fetch("SELECT * FROM identity.email_outbox")
    assert (out["template"], out["password_reset_id"], out["recipient"]) == (
        "password_reset",
        reset["id"],
        "ann@example.com",
    )
    [audit] = await admin_db.fetch(
        "SELECT * FROM identity.audit_log WHERE event = 'auth.password_reset.requested'"
    )
    assert (audit["target_user_id"], audit["actor_user_id"]) == (ann, None)
    assert json.loads(audit["details"]) == {"reset_id": str(reset["id"])}
    assert audit["ip_hash"] == ip_hash(CLIENT_IP, AUDIT_KEY.encode())
    matched = [e["matched"] for e in caplog_json.events("auth.password_reset.requested")]
    assert matched == [True, False, False, False]
    assert len(caplog_json.events("email.queued")) == 1


async def test_confirm_sets_the_password_and_bumps_roles_version(
    harness: HarnessFactory, admin_db: Any, caplog_json: JsonLogCapture
) -> None:
    """PAY-105, PAY-107, R19, C31: the relay issues the token at send (expires_at = send + 1 h);
    confirm returns {user_id, roles_version}, bumps roles_version by one, replaces the
    credential (old password fails, new works), marks the reset used and audits."""
    async with harness() as h:
        ann = await h.create_user("ann@example.com", password=PW)
        before = await admin_db.fetchval(
            "SELECT password_changed_at FROM identity.credentials WHERE user_id = $1", ann
        )
        token = await requested_reset_token(h, "ann@example.com")
        reset = await admin_db.fetchrow("SELECT * FROM identity.password_resets")
        assert reset["expires_at"] - reset["token_issued_at"] == timedelta(hours=1)
        r = await h.reset_confirm(token, NEW_PW)
        assert r.status_code == 200, r.text
        assert r.json() == {"user_id": str(ann), "roles_version": 2}
        assert (await h.login("ann@example.com", PW)).status_code == 401
        login = await h.login("ann@example.com", NEW_PW)
        assert login.status_code == 200
        assert login.json()["roles_version"] == 2
        me = await h.client.get("/v1/users/me", headers=h.user_headers(ann))
        assert me.json()["roles_version"] == 2
    row = await admin_db.fetchrow("SELECT status, used_at FROM identity.password_resets")
    assert row["status"] == "used"
    assert row["used_at"] is not None
    after = await admin_db.fetchval(
        "SELECT password_changed_at FROM identity.credentials WHERE user_id = $1", ann
    )
    assert after > before
    audit = await admin_db.fetchrow(
        "SELECT * FROM identity.audit_log WHERE event = 'auth.password_reset.completed'"
    )
    assert (audit["actor_user_id"], audit["target_user_id"]) == (ann, ann)
    assert json.loads(audit["details"]) == {"reset_id": str(reset["id"])}
    [done] = caplog_json.events("auth.password_reset.completed")
    assert (done["user_id"], done["roles_version"]) == (str(ann), 2)


async def test_confirm_for_a_user_without_a_credential_creates_one(
    harness: HarnessFactory, admin_db: Any
) -> None:
    """PAY-105: confirm upserts the credential, so an active user with no stored hash (none
    exists in normal flows) can still set a password."""
    async with harness() as h:
        await h.create_user("ann@example.com")
        token = await requested_reset_token(h, "ann@example.com")
        assert (await h.reset_confirm(token, NEW_PW)).status_code == 200
        assert (await h.login("ann@example.com", NEW_PW)).status_code == 200
    assert await admin_db.fetchval("SELECT count(*) FROM identity.credentials") == 1


async def test_invalid_tokens_are_reset_invalid(
    harness: HarnessFactory, admin_db: Any, caplog_json: JsonLogCapture
) -> None:
    """PAY-105: malformed, unknown, expired, used and revoked tokens, and a user who is no
    longer active, → 400 reset_invalid; the token is checked before the password policy; a
    weak password with a good token → 422 password_too_weak."""
    async with harness() as h:
        ann = await h.create_user("ann@example.com", password=PW)
        token = await requested_reset_token(h, "ann@example.com")

        def invalid(r: Any) -> bool:
            return (r.status_code, r.json()["error"]["code"]) == (400, "reset_invalid")

        assert invalid(await h.reset_confirm("short", "x"))
        assert invalid(await h.reset_confirm("A" * 43, NEW_PW))
        r = await h.reset_confirm(token, "x" * 11)
        assert (r.status_code, r.json()["error"]["code"]) == (422, "password_too_weak")

        await admin_db.execute(
            "UPDATE identity.password_resets SET expires_at = now() - interval '1 second'"
        )
        assert invalid(await h.reset_confirm(token, NEW_PW))
        await admin_db.execute(
            "UPDATE identity.password_resets SET expires_at = now() + interval '1 hour'"
        )

        await admin_db.execute("UPDATE identity.users SET status = 'inactive' WHERE id = $1", ann)
        assert invalid(await h.reset_confirm(token, NEW_PW))
        await admin_db.execute("UPDATE identity.users SET status = 'active' WHERE id = $1", ann)

        assert (await h.reset_confirm(token, NEW_PW)).status_code == 200
        assert invalid(await h.reset_confirm(token, NEW_PW))  # used

        # A newer request revokes the pending reset: its token dies.
        older = await requested_reset_token(h, "ann@example.com")
        newer = await requested_reset_token(h, "ann@example.com")
        assert invalid(await h.reset_confirm(older, NEW_PW))
        assert (await h.reset_confirm(newer, NEW_PW)).status_code == 200
    reasons = [e["reason"] for e in caplog_json.events("auth.password_reset.invalid")]
    assert reasons == [
        "malformed",
        "unknown",
        "expired",
        "user_not_active",
        "not_pending",
        "not_pending",
    ]
    statuses = [
        r["status"]
        for r in await admin_db.fetch("SELECT status FROM identity.password_resets ORDER BY id")
    ]
    assert statuses == ["used", "revoked", "used"]


async def test_a_new_request_cancels_the_unsent_email(
    harness: HarnessFactory, admin_db: Any
) -> None:
    """PAY-105, PAY-110: a second request before the first email went out revokes the first
    reset and cancels its outbox row, so only one email is sent."""
    async with harness() as h:
        await h.create_user("ann@example.com", password=PW)
        assert (await h.reset_request("ann@example.com")).status_code == 202
        assert (await h.reset_request("ann@example.com")).status_code == 202
        await h.rt.relay.run_once()
        assert h.sender is not None
        assert len(h.sender.messages) == 1
    rows = await admin_db.fetch("SELECT status FROM identity.email_outbox ORDER BY id")
    assert [r["status"] for r in rows] == ["cancelled", "sent"]


async def test_concurrent_confirms_one_wins(harness: HarnessFactory) -> None:
    """PAY-105: two concurrent confirms of one token → exactly one 200."""
    async with harness() as h:
        await h.create_user("ann@example.com", password=PW)
        token = await requested_reset_token(h, "ann@example.com")
        a, b = await asyncio.gather(h.reset_confirm(token, NEW_PW), h.reset_confirm(token, NEW_PW))
    assert sorted([a.status_code, b.status_code]) == [200, 400]


async def test_reset_requests_are_rate_limited_like_login(
    harness: HarnessFactory, otel_memory: OtelMemory
) -> None:
    """PAY-105, PAY-603: reset requests count per account (AUTH_LOGIN_LIMIT_ACCOUNT) and per IP
    (AUTH_LOGIN_LIMIT_IP), unknown emails included; past either → 429 with Retry-After."""
    async with harness(auth_login_limit_account=2, auth_login_limit_ip=4) as h:
        await h.create_user("ann@example.com", password=PW)
        codes = [(await h.reset_request("ann@example.com")).status_code for _ in range(3)]
        assert codes == [202, 202, 429]
        r = await h.reset_request("ann@example.com", ip="192.0.2.50")
        assert r.status_code == 429  # the account limit holds across IPs
        assert 1 <= int(r.headers["retry-after"]) <= 900
        r = await h.reset_request("nobody@example.com", ip="192.0.2.60")
        assert r.status_code == 202
        # 192.0.2.60: one hit so far; three more emails reach its IP limit of four.
        codes = [
            (await h.reset_request(f"n{i}@example.com", ip="192.0.2.60")).status_code
            for i in range(4)
        ]
        assert codes == [202, 202, 202, 429]
    limited = {a["scope"]: v for a, v in otel_memory.metric_points("rate_limited")}
    assert limited["reset_account"] == 2
    assert limited["reset_ip"] == 1


async def test_confirm_shares_the_accept_ip_limit(harness: HarnessFactory) -> None:
    """PAY-603, R18: reset confirm and invite accept share accept_ip at AUTH_LOGIN_LIMIT_IP;
    requests without X-Client-IP share the 'unknown' bucket (C16)."""
    async with harness(auth_login_limit_ip=2) as h:
        assert (await h.reset_confirm("A" * 43, NEW_PW, ip=None)).status_code == 400
        assert (await h.accept("A" * 43, NEW_PW)).status_code == 400  # another IP's bucket
        r = await h.client.post(
            "/v1/invites/accept",
            json={"token": "A" * 43, "password": NEW_PW},
            headers=h.web_headers(ip=None),
        )
        assert r.status_code == 400
        r = await h.reset_confirm("A" * 43, NEW_PW, ip=None)
        assert (r.status_code, r.json()["error"]["code"]) == (429, "rate_limited")


async def test_reset_routes_reject_user_tokens(harness: HarnessFactory) -> None:
    """PAY-601, C30: the reset routes accept only service:web."""
    async with harness() as h:
        uid = await h.create_user("ann@example.com", password=PW)
        for path, body in (
            ("/v1/auth/password-reset/request", {"email": "ann@example.com"}),
            ("/v1/auth/password-reset/confirm", {"token": "t", "password": NEW_PW}),
        ):
            r = await h.client.post(path, json=body, headers=h.user_headers(uid, ("admin",)))
            assert (r.status_code, r.json()["error"]["code"]) == (403, "forbidden_role")


async def test_deactivation_revokes_a_pending_reset(harness: HarnessFactory, admin_db: Any) -> None:
    """PAY-105, PAY-108: deactivating a user revokes their pending reset: a token already sent
    is then reset_invalid, and an email not yet sent is cancelled."""
    async with harness() as h:
        admin = await h.create_user("admin@example.com", password=PW)
        ann = await h.create_user("ann@example.com", password=PW, roles=("approver",))
        bob = await h.create_user("bob@example.com", password=PW, roles=("approver",))
        token = await requested_reset_token(h, "ann@example.com")
        assert (await h.reset_request("bob@example.com")).status_code == 202  # not sent yet
        for uid in (ann, bob):
            assert (await h.patch_user(admin, uid, {"status": "inactive"})).status_code == 200
        r = await h.reset_confirm(token, NEW_PW)
        assert (r.status_code, r.json()["error"]["code"]) == (400, "reset_invalid")
        await h.rt.relay.run_once()
        assert h.sender is not None
        assert len(h.sender.messages) == 1  # only ann's, sent before the deactivation
    resets = await admin_db.fetch(
        "SELECT status, revoked_at FROM identity.password_resets ORDER BY id"
    )
    assert [r["status"] for r in resets] == ["revoked", "revoked"]
    assert all(r["revoked_at"] is not None for r in resets)
    outbox = [
        r["status"]
        for r in await admin_db.fetch("SELECT status FROM identity.email_outbox ORDER BY id")
    ]
    assert outbox == ["sent", "cancelled"]
