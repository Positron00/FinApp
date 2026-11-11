"""Invite and accept (identity LLD §6.6-6.7, §7.2; R17, C6, C11, C31)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from identity.domain.ip import ip_hash
from pycommon.testing import JsonLogCapture

from tests.helpers import AUDIT_KEY, COMPANY_ID
from tests.integration.conftest import CLIENT_IP, Harness, HarnessFactory, token_in

PW = "correct horse battery staple"
NEW_PW = "a brand new passphrase"


async def invite(
    h: Harness, admin: UUID, email: str = "Bea@Example.com", roles: tuple[str, ...] = ("approver",)
) -> Any:
    return await h.client.post(
        "/v1/users/invite",
        json={"email": email, "name": "  Bea  ", "roles": list(roles)},
        # The BFF sends X-Client-IP on every identity call when it knows the IP (C16).
        headers={**h.user_headers(admin, ("admin",)), "X-Client-IP": CLIENT_IP},
    )


async def send_and_get_token(h: Harness) -> str:
    assert h.sender is not None
    await h.rt.relay.run_once()
    return token_in(h.sender.messages[-1])


async def test_invite_creates_pending_user_invite_and_outbox_row(
    harness: HarnessFactory, admin_db: Any
) -> None:
    """PAY-101, C11: 201 UserOut (pending, pending_invite_id), a pending outbox row with no
    token, and audit user.invited with the client's ip_hash (PAY-NFR-08); the invite's
    expires_at is NULL until sent (C31)."""
    async with harness() as h:
        admin = await h.create_user("admin@example.com", password=PW)
        r = await invite(h, admin, roles=("payroll_admin", "approver"))
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending"
    assert body["email"] == "Bea@example.com"
    assert body["name"] == "Bea"
    assert body["roles"] == ["approver", "payroll_admin"]
    assert body["roles_version"] == 1
    assert body["activated_at"] is None
    inv = await admin_db.fetchrow("SELECT * FROM identity.invites")
    assert body["pending_invite_id"] == str(inv["id"])
    assert inv["token_hash"] is None
    assert inv["expires_at"] is None
    assert inv["created_by"] == admin
    out = await admin_db.fetchrow("SELECT * FROM identity.email_outbox")
    assert (out["status"], out["template"], out["invite_id"]) == ("pending", "invite", inv["id"])
    assert out["recipient"] == "Bea@example.com"
    audit = await admin_db.fetchrow("SELECT * FROM identity.audit_log WHERE event = 'user.invited'")
    assert audit["actor_user_id"] == admin
    assert str(audit["target_user_id"]) == body["id"]
    assert audit["ip_hash"] == ip_hash(CLIENT_IP, AUDIT_KEY.encode())


async def test_duplicate_email_is_409_case_insensitive(harness: HarnessFactory) -> None:
    """PAY-101: 409 user_email_taken for any existing user with that email, any case, also
    for two concurrent invites."""
    async with harness() as h:
        admin = await h.create_user("admin@example.com", password=PW)
        await h.create_user("gone@example.com", status="inactive")
        assert (await invite(h, admin)).status_code == 201
        for email in ("bea@EXAMPLE.com", "GONE@example.com", "Admin@Example.com"):
            r = await invite(h, admin, email=email)
            assert r.status_code == 409
            assert r.json()["error"]["code"] == "user_email_taken"
        a, b = await asyncio.gather(
            invite(h, admin, email="cy@example.com"), invite(h, admin, email="CY@example.com")
        )
        assert sorted([a.status_code, b.status_code]) == [201, 409]


@pytest.mark.parametrize(
    ("body", "field", "issue"),
    [
        ({"email": "a@b.example", "name": "A", "roles": []}, "body.roles", "too_short"),
        ({"email": "a@b.example", "name": "A", "roles": ["boss"]}, "body.roles.0", "enum"),
        (
            {"email": "a@b.example", "name": "A", "roles": ["admin", "admin"]},
            "body.roles",
            "value_error",
        ),
        ({"email": "nope", "name": "A", "roles": ["admin"]}, "body.email", "value_error"),
        (
            {"email": "a@b.example", "name": " ", "roles": ["admin"]},
            "body.name",
            "string_too_short",
        ),
        (
            {"email": "a@b.example", "name": "A\x00B", "roles": ["admin"]},
            "body.name",
            "invalid_character",
        ),
        (
            {"email": "a\x00@b.example", "name": "A", "roles": ["admin"]},
            "body.email",
            "invalid_character",
        ),
    ],
)
async def test_invite_validation(
    harness: HarnessFactory, body: dict[str, object], field: str, issue: str
) -> None:
    """PAY-101, PAY-602, C63: empty, unknown or duplicate roles, bad email or name, and NUL
    characters → 422 (the input is never echoed)."""
    async with harness() as h:
        admin = await h.create_user("admin@example.com", password=PW)
        r = await h.client.post(
            "/v1/users/invite", json=body, headers=h.user_headers(admin, ("admin",))
        )
    assert r.status_code == 422
    assert {"field": field, "issue": issue} in r.json()["error"]["details"]


async def test_non_admin_is_403_even_with_a_stale_admin_jwt(
    harness: HarnessFactory, caplog_json: JsonLogCapture
) -> None:
    """PAY-101, PAY-601: the admin check reads the DB roles and status, not the JWT, and each
    denial logs auth.forbidden (reason db_role)."""
    async with harness() as h:
        approver = await h.create_user("ap@example.com", password=PW, roles=("approver",))
        gone = await h.create_user("gone@example.com", password=PW, status="inactive")
        pending = await h.create_user("p@example.com", status="pending")
        ghost = UUID("0193a000-0000-7000-8000-0000000000ff")
        for uid in (approver, gone, pending, ghost):
            r = await invite(h, uid)
            assert r.status_code == 403
            assert r.json()["error"]["code"] == "forbidden_role"
    denied = caplog_json.events("auth.forbidden")
    assert [(e["reason"], e["route"]) for e in denied] == [("db_role", "/v1/users/invite")] * 4


async def test_accept_activates_and_returns_session_user(
    harness: HarnessFactory, admin_db: Any
) -> None:
    """PAY-103, PAY-106, C6, C31: the relay issues the token at send (expires_at = send + 72 h);
    accept returns SessionUser, activates the user, stores an argon2id hash and audits."""
    async with harness() as h:
        admin = await h.create_user("admin@example.com", password=PW)
        uid = (await invite(h, admin)).json()["id"]
        token = await send_and_get_token(h)
        inv = await admin_db.fetchrow("SELECT * FROM identity.invites")
        assert inv["token_hash"] is not None
        delta = inv["expires_at"] - inv["token_issued_at"]
        assert delta == timedelta(hours=72)
        r = await h.accept(token, NEW_PW)
        assert r.status_code == 200, r.text
        assert r.json() == {
            "user_id": uid,
            "company_id": str(COMPANY_ID),
            "email": "Bea@example.com",
            "name": "Bea",
            "roles": ["approver"],
            "roles_version": 1,
            "status": "active",
        }
        assert (await h.login("bea@example.com", NEW_PW)).status_code == 200
        me = await h.client.get("/v1/users/me", headers=h.user_headers(UUID(uid)))
        assert me.json()["pending_invite_id"] is None
    user = await admin_db.fetchrow("SELECT * FROM identity.users WHERE id = $1", UUID(uid))
    assert user["status"] == "active"
    assert user["activated_at"] is not None
    cred = await admin_db.fetchval("SELECT password_hash FROM identity.credentials")
    assert cred.startswith("$argon2id$")
    assert await admin_db.fetchval("SELECT status FROM identity.invites") == "accepted"
    audit = await admin_db.fetchrow(
        "SELECT * FROM identity.audit_log WHERE event = 'user.invite_accepted'"
    )
    assert str(audit["actor_user_id"]) == uid
    assert audit["ip_hash"] is not None


async def test_resend_moves_expiry_forward(harness: HarnessFactory, admin_db: Any) -> None:
    """PAY-101, R17, C31: each send replaces the token and restarts the TTL; the old link dies."""
    async with harness() as h:
        admin = await h.create_user("admin@example.com", password=PW)
        await invite(h, admin)
        first = await send_and_get_token(h)
        # Age the first send by a minute instead of sleeping, so the re-send is visibly later.
        before = await admin_db.fetchval(
            "UPDATE identity.invites SET token_issued_at = token_issued_at - interval '1 minute', "
            "expires_at = expires_at - interval '1 minute' RETURNING expires_at"
        )
        await admin_db.execute(
            "UPDATE identity.email_outbox SET status = 'pending', next_attempt_at = now()"
        )
        second = await send_and_get_token(h)
        after = await admin_db.fetchval("SELECT expires_at FROM identity.invites")
        assert second != first
        assert after - before >= timedelta(seconds=59)
        assert (await h.accept(first, NEW_PW)).json()["error"]["code"] == "invite_invalid"
        assert (await h.accept(second, NEW_PW)).status_code == 200


async def test_accept_rejections(harness: HarnessFactory, admin_db: Any) -> None:
    """PAY-103: malformed, unknown, expired and used tokens → 400 invite_invalid; the token is
    checked before the policy; an 11-character password → 422 password_too_weak."""
    async with harness() as h:
        admin = await h.create_user("admin@example.com", password=PW)
        await invite(h, admin)
        token = await send_and_get_token(h)

        for bad in ("short", "A" * 43, "A" * 42 + "B"):
            r = await h.accept(bad, "x" * 5)
            assert (r.status_code, r.json()["error"]["code"]) == (400, "invite_invalid")
        r = await h.accept(token, "x" * 11)
        assert (r.status_code, r.json()["error"]["code"]) == (422, "password_too_weak")

        await admin_db.execute(
            "UPDATE identity.invites SET expires_at = now() - interval '1 second'"
        )
        r = await h.accept(token, NEW_PW)
        assert (r.status_code, r.json()["error"]["code"]) == (400, "invite_invalid")
        await admin_db.execute("UPDATE identity.invites SET expires_at = now() + interval '1 h'")

        assert (await h.accept(token, NEW_PW)).status_code == 200
        r = await h.accept(token, NEW_PW)
        assert (r.status_code, r.json()["error"]["code"]) == (400, "invite_invalid")


async def test_user_no_longer_pending_and_revoked_invite(
    harness: HarnessFactory, admin_db: Any
) -> None:
    """PAY-103: a revoked invite, or a user who is no longer pending, → 400 invite_invalid."""
    async with harness() as h:
        admin = await h.create_user("admin@example.com", password=PW)
        await invite(h, admin)
        token = await send_and_get_token(h)
        await admin_db.execute(
            "UPDATE identity.users SET status = 'inactive' WHERE email LIKE 'Bea%'"
        )
        assert (await h.accept(token, NEW_PW)).status_code == 400
        await admin_db.execute(
            "UPDATE identity.users SET status = 'pending' WHERE email LIKE 'Bea%'"
        )
        await admin_db.execute("UPDATE identity.invites SET status = 'revoked'")
        assert (await h.accept(token, NEW_PW)).status_code == 400


async def test_concurrent_accepts_one_wins(harness: HarnessFactory) -> None:
    """PAY-103: two concurrent accepts of one token → exactly one 200."""
    async with harness() as h:
        admin = await h.create_user("admin@example.com", password=PW)
        await invite(h, admin)
        token = await send_and_get_token(h)
        a, b = await asyncio.gather(h.accept(token, NEW_PW), h.accept(token, NEW_PW))
    assert sorted([a.status_code, b.status_code]) == [200, 400]


async def test_accept_is_public_only_and_ip_limited(harness: HarnessFactory) -> None:
    """PAY-603, R18, C16: accept is limited per IP at AUTH_LOGIN_LIMIT_IP; requests without
    X-Client-IP share the 'unknown' bucket; a user token is 403."""
    async with harness(auth_login_limit_ip=2) as h:
        admin = await h.create_user("admin@example.com", password=PW)
        r = await h.client.post(
            "/v1/invites/accept",
            json={"token": "x", "password": NEW_PW},
            headers=h.user_headers(admin),
        )
        assert r.status_code == 403
        for _ in range(2):
            r = await h.client.post(
                "/v1/invites/accept",
                json={"token": "x", "password": NEW_PW},
                headers=h.web_headers(ip=None),
            )
            assert r.status_code == 400
        r = await h.client.post(
            "/v1/invites/accept",
            json={"token": "x", "password": NEW_PW},
            headers=h.web_headers(ip=None),
        )
        assert r.status_code == 429
        assert int(r.headers["retry-after"]) >= 1
        # Another IP still has its own bucket.
        assert (await h.accept("x", NEW_PW)).status_code == 400
