"""``GET /v1/audit-log`` (identity LLD §3.4, §7.2; PAY-109, PAY-NFR-08)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from tests.helpers import passphrase
from tests.integration.conftest import CLIENT_IP, Harness, HarnessFactory, token_in

ADMIN_PW = passphrase()
NEW_PW = passphrase()
RESET_PW = passphrase()
WRONG_PW = passphrase()
INVITEE = "invitee.person@example.com"


async def audit_page(h: Harness, admin: UUID, **params: Any) -> Any:
    return await h.client.get("/v1/audit-log", params=params, headers=h.admin_headers(admin))


async def test_full_journey_is_audited_without_secrets(
    harness: HarnessFactory, admin_db: Any
) -> None:
    """PAY-109, PAY-NFR-08: after invite, accept, a failed and a good sign-in, a password reset
    and a role change, the log lists those events newest first, and no column of any row holds
    a password, token, email or raw IP."""
    async with harness() as h:
        admin = await h.create_user("admin.person@example.com", password=ADMIN_PW)
        r = await h.client.post(
            "/v1/users/invite",
            json={"email": INVITEE, "name": "Invitee", "roles": ["approver"]},
            headers=h.admin_headers(admin),
        )
        uid = UUID(r.json()["id"])
        await h.rt.relay.run_once()
        assert h.sender is not None
        invite_token = token_in(h.sender.messages[-1])
        assert (await h.accept(invite_token, NEW_PW)).status_code == 200
        assert (await h.login(INVITEE, WRONG_PW)).status_code == 401
        assert (await h.login(INVITEE, NEW_PW)).status_code == 200
        assert (await h.reset_request(INVITEE)).status_code == 202
        await h.rt.relay.run_once()
        reset_token = token_in(h.sender.messages[-1])
        assert (await h.reset_confirm(reset_token, RESET_PW)).status_code == 200
        assert (await h.patch_user(admin, uid, {"roles": ["payroll_admin"]})).status_code == 200

        r = await audit_page(h, admin)
        assert r.status_code == 200
        items = r.json()["items"]
        assert r.json()["next_cursor"] is None
    events = [e["event"] for e in items]
    assert events == [
        "user.roles_changed",
        "auth.password_reset.completed",
        "auth.password_reset.requested",
        "auth.signin.succeeded",
        "auth.signin.failed",
        "user.invite_accepted",
        "user.invited",
    ]
    assert set(items[0]) == {
        "id",
        "at",
        "actor_user_id",
        "event",
        "target_user_id",
        "ip_hash",
        "details",
    }
    assert items[4]["details"] == {"reason": "bad_password"}
    assert all(e["ip_hash"] is not None and len(e["ip_hash"]) == 64 for e in items)
    dump = " ".join(str(dict(r)) for r in await admin_db.fetch("SELECT * FROM identity.audit_log"))
    for secret in (
        ADMIN_PW,
        NEW_PW,
        RESET_PW,
        WRONG_PW,
        invite_token,
        reset_token,
        INVITEE,
        "admin.person@example.com",
        CLIENT_IP,
    ):
        assert secret not in dump


async def test_newest_first_with_cursor_paging(harness: HarnessFactory, admin_db: Any) -> None:
    """PAY-109, R4: id DESC (newest first); the cursor continues after the last row, the last
    page's next_cursor is null; limit is 1-100 and a bad cursor is 422."""
    async with harness() as h:
        admin = await h.create_user("admin@example.com", password=ADMIN_PW)
        for _ in range(5):
            assert (await h.login("admin@example.com", WRONG_PW)).status_code == 401
        seen: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 2}
            if cursor is not None:
                params["cursor"] = cursor
            body = (await audit_page(h, admin, **params)).json()
            seen.extend(e["id"] for e in body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                break
        assert (await audit_page(h, admin, limit=101)).status_code == 422
        r = await audit_page(h, admin, cursor="?")
        assert r.json()["error"]["details"] == [
            {"field": "query.cursor", "issue": "invalid_cursor"}
        ]
    ids = [
        str(r["id"])
        for r in await admin_db.fetch("SELECT id FROM identity.audit_log ORDER BY id DESC")
    ]
    assert seen == ids
    assert len(ids) == 5


async def test_audit_log_is_admin_only_and_company_scoped(
    harness: HarnessFactory, admin_db: Any
) -> None:
    """PAY-109, PAY-601: a non-admin (DB roles) or service:web → 403; another company's rows
    are never listed."""
    async with harness() as h:
        admin = await h.create_user("admin@example.com", password=ADMIN_PW)
        approver = await h.create_user("ap@example.com", password=ADMIN_PW, roles=("approver",))
        assert (await h.login("ap@example.com", WRONG_PW)).status_code == 401
        await admin_db.execute(
            "UPDATE identity.audit_log SET company_id = '0193a000-0000-7000-8000-0000000000ee'"
        )
        assert (await h.login("ap@example.com", WRONG_PW)).status_code == 401
        r = await audit_page(h, approver)
        assert (r.status_code, r.json()["error"]["code"]) == (403, "forbidden_role")
        r = await h.client.get("/v1/audit-log", headers=h.web_headers())
        assert r.status_code == 403
        r = await audit_page(h, admin)
        assert len(r.json()["items"]) == 1
