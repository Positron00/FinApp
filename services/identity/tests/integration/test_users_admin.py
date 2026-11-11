"""User admin: list and PATCH (identity LLD §3.4, §6.5, §7.2; PAY-107, PAY-108, PAY-601)."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID

import pytest
from identity.domain.ip import ip_hash
from pycommon.testing import JsonLogCapture

from tests.helpers import AUDIT_KEY, passphrase
from tests.integration.conftest import CLIENT_IP, Harness, HarnessFactory, token_in

PW = passphrase()


async def list_users(h: Harness, admin: UUID, **params: Any) -> Any:
    return await h.client.get("/v1/users", params=params, headers=h.admin_headers(admin))


async def audit_rows(admin_db: Any, event: str) -> list[Any]:
    rows: list[Any] = await admin_db.fetch(
        "SELECT * FROM identity.audit_log WHERE event = $1 ORDER BY id", event
    )
    return rows


async def test_list_pages_in_creation_order(harness: HarnessFactory) -> None:
    """PAY-108, R4, R21, C11: {items, next_cursor} in creation order; the cursor continues
    after the last row and is null on the last page; pending users carry pending_invite_id."""
    async with harness() as h:
        admin = await h.create_user("admin@example.com", password=PW)
        created = [admin]
        created += [await h.create_user(f"u{i}@example.com", roles=("approver",)) for i in range(4)]
        r = await h.client.post(
            "/v1/users/invite",
            json={"email": "p@example.com", "name": "P", "roles": ["approver"]},
            headers=h.admin_headers(admin),
        )
        invited = r.json()
        created.append(UUID(invited["id"]))

        seen: list[dict[str, Any]] = []
        cursor: str | None = None
        pages = 0
        while True:
            params: dict[str, Any] = {"limit": 2}
            if cursor is not None:
                params["cursor"] = cursor
            r = await list_users(h, admin, **params)
            assert r.status_code == 200
            body = r.json()
            assert set(body) == {"items", "next_cursor"}
            assert len(body["items"]) <= 2
            seen.extend(body["items"])
            pages += 1
            cursor = body["next_cursor"]
            if cursor is None:
                break
        assert pages == 3
        assert [UUID(u["id"]) for u in seen] == created
        by_id = {u["id"]: u for u in seen}
        assert by_id[invited["id"]]["pending_invite_id"] == invited["pending_invite_id"]
        assert all(u["pending_invite_id"] is None for u in seen if u["status"] != "pending")

        full = (await list_users(h, admin)).json()
        assert (len(full["items"]), full["next_cursor"]) == (6, None)


@pytest.mark.parametrize(
    ("params", "field", "issue"),
    [
        ({"limit": 101}, "query.limit", "less_than_equal"),
        ({"limit": 0}, "query.limit", "greater_than_equal"),
        ({"cursor": "not-a-cursor"}, "query.cursor", "invalid_cursor"),
        ({"cursor": "x" * 65}, "query.cursor", "string_too_long"),
    ],
)
async def test_list_validation(
    harness: HarnessFactory, params: dict[str, Any], field: str, issue: str
) -> None:
    """PAY-108, PAY-602, C14: limit is 1-100 and a bad cursor is 422 validation_error."""
    async with harness() as h:
        admin = await h.create_user("admin@example.com", password=PW)
        r = await list_users(h, admin, **params)
    assert r.status_code == 422
    assert r.json()["error"]["details"] == [{"field": field, "issue": issue}]


async def test_admin_routes_need_a_db_admin(
    harness: HarnessFactory, caplog_json: JsonLogCapture
) -> None:
    """PAY-108, PAY-601: list and PATCH are 403 for a non-admin, also when its JWT still says
    admin (roles are re-read from the DB)."""
    async with harness() as h:
        approver = await h.create_user("ap@example.com", password=PW, roles=("approver",))
        other = await h.create_user("o@example.com", roles=("approver",))
        r = await list_users(h, approver)
        assert (r.status_code, r.json()["error"]["code"]) == (403, "forbidden_role")
        r = await h.patch_user(approver, other, {"status": "inactive"})
        assert (r.status_code, r.json()["error"]["code"]) == (403, "forbidden_role")
    routes = [e["route"] for e in caplog_json.events("auth.forbidden")]
    assert routes == ["/v1/users", "/v1/users/{id}"]


async def test_patch_roles_bumps_roles_version_and_audits(
    harness: HarnessFactory, admin_db: Any, caplog_json: JsonLogCapture
) -> None:
    """PAY-107, PAY-108, PAY-NFR-08: a new role set is stored sorted, bumps roles_version by
    one and audits user.roles_changed (old and new roles, ip_hash); the same set again (any
    order) is a 200 no-op with no bump and no audit."""
    async with harness() as h:
        admin = await h.create_user("admin@example.com", password=PW)
        bea = await h.create_user("bea@example.com", password=PW, roles=("approver",))
        r = await h.patch_user(admin, bea, {"roles": ["payroll_admin", "approver"]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["roles"], body["roles_version"]) == (["approver", "payroll_admin"], 2)
        r = await h.patch_user(admin, bea, {"roles": ["approver", "payroll_admin"]})
        assert (r.status_code, r.json()["roles_version"]) == (200, 2)
        me = await h.client.get("/v1/users/me", headers=h.user_headers(bea))
        assert me.json()["roles_version"] == 2
    [audit] = await audit_rows(admin_db, "user.roles_changed")
    assert (audit["actor_user_id"], audit["target_user_id"]) == (admin, bea)
    assert json.loads(audit["details"]) == {
        "old_roles": ["approver"],
        "new_roles": ["approver", "payroll_admin"],
    }
    assert audit["ip_hash"] == ip_hash(CLIENT_IP, AUDIT_KEY.encode())
    [log] = caplog_json.events("user.roles_changed")
    assert (log["roles_version"], log["new_roles"]) == (2, ["approver", "payroll_admin"])


async def test_deactivate_bumps_audits_and_ends_sign_in(
    harness: HarnessFactory, admin_db: Any
) -> None:
    """PAY-107, PAY-108: status inactive bumps roles_version, sets deactivated_at, audits
    user.deactivated; the user can no longer sign in and /me shows inactive; deactivating again
    is a no-op. Roles and status in one PATCH bump once and audit both."""
    async with harness() as h:
        admin = await h.create_user("admin@example.com", password=PW)
        bea = await h.create_user("bea@example.com", password=PW, roles=("approver",))
        cy = await h.create_user("cy@example.com", password=PW, roles=("approver",))
        r = await h.patch_user(admin, bea, {"status": "inactive"})
        assert r.status_code == 200
        body = r.json()
        assert (body["status"], body["roles_version"]) == ("inactive", 2)
        assert body["deactivated_at"] is not None
        assert (await h.login("bea@example.com", PW)).status_code == 401
        me = (await h.client.get("/v1/users/me", headers=h.user_headers(bea))).json()
        assert (me["status"], me["roles_version"]) == ("inactive", 2)
        again = await h.patch_user(admin, bea, {"status": "inactive"})
        assert (again.status_code, again.json()["roles_version"]) == (200, 2)

        both = await h.patch_user(admin, cy, {"roles": ["payroll_admin"], "status": "inactive"})
        assert (both.json()["roles_version"], both.json()["roles"]) == (2, ["payroll_admin"])
    assert [r["target_user_id"] for r in await audit_rows(admin_db, "user.deactivated")] == [
        bea,
        cy,
    ]
    assert [r["target_user_id"] for r in await audit_rows(admin_db, "user.roles_changed")] == [cy]


async def test_deactivating_a_pending_user_revokes_the_invite(
    harness: HarnessFactory, admin_db: Any, caplog_json: JsonLogCapture
) -> None:
    """PAY-108, PAY-103: deactivating a pending user revokes the pending invite and cancels its
    unsent email; pending_invite_id is then null and the invite can't be resent."""
    async with harness() as h:
        admin = await h.create_user("admin@example.com", password=PW)
        r = await h.client.post(
            "/v1/users/invite",
            json={"email": "p@example.com", "name": "P", "roles": ["approver"]},
            headers=h.admin_headers(admin),
        )
        user_id, invite_id = r.json()["id"], r.json()["pending_invite_id"]
        r = await h.patch_user(admin, UUID(user_id), {"status": "inactive"})
        assert r.status_code == 200
        assert (r.json()["status"], r.json()["pending_invite_id"]) == ("inactive", None)
        r = await h.client.post(f"/v1/invites/{invite_id}/resend", headers=h.admin_headers(admin))
        assert (r.status_code, r.json()["error"]["code"]) == (409, "invite_not_pending")
    assert await admin_db.fetchval("SELECT status FROM identity.invites") == "revoked"
    assert await admin_db.fetchval("SELECT status FROM identity.email_outbox") == "cancelled"
    [log] = caplog_json.events("user.deactivated")
    assert log["revoked_invite_id"] == invite_id


@pytest.mark.parametrize(
    ("body", "field", "issue"),
    [
        ({"status": "active"}, "body.status", "literal_error"),
        ({}, "body", "value_error"),
        ({"roles": []}, "body.roles", "too_short"),
        ({"roles": ["approver", "approver"]}, "body.roles", "value_error"),
        ({"email": "x@example.com"}, "body.email", "extra_forbidden"),
    ],
)
async def test_patch_validation(
    harness: HarnessFactory, body: dict[str, Any], field: str, issue: str
) -> None:
    """PAY-108, R37, C25: only status "inactive" (no reactivation), at least one field, valid
    roles and no other fields → else 422 validation_error."""
    async with harness() as h:
        admin = await h.create_user("admin@example.com", password=PW)
        gone = await h.create_user("g@example.com", status="inactive", roles=("approver",))
        r = await h.patch_user(admin, gone, body)
    assert r.status_code == 422
    assert {"field": field, "issue": issue} in r.json()["error"]["details"]


async def test_self_and_unknown_targets(
    harness: HarnessFactory, admin_db: Any, caplog_json: JsonLogCapture
) -> None:
    """PAY-108, PAY-602: PATCH on yourself → 409 cannot_modify_self (also a no-op patch); an
    unknown user → 404 user_not_found; nothing is written."""
    async with harness() as h:
        admin = await h.create_user("admin@example.com", password=PW)
        await h.create_user("other@example.com", password=PW)  # another admin
        for body in ({"status": "inactive"}, {"roles": ["admin"]}):
            r = await h.patch_user(admin, admin, body)
            assert (r.status_code, r.json()["error"]["code"]) == (409, "cannot_modify_self")
        ghost = UUID("0193a000-0000-7000-8000-0000000000ff")
        r = await h.patch_user(admin, ghost, {"status": "inactive"})
        assert (r.status_code, r.json()["error"]["code"]) == (404, "user_not_found")
    assert await admin_db.fetchval("SELECT max(roles_version) FROM identity.users") == 1
    refused = [(e["code"], e["user_id"]) for e in caplog_json.events("user.update_refused")]
    assert refused == [("cannot_modify_self", str(admin))] * 2


async def test_two_admins_demoting_each_other_never_leave_zero_admins(
    harness: HarnessFactory, admin_db: Any
) -> None:
    """PAY-108: two admins demote (or deactivate) each other at once: the admin set is locked
    first, so one wins (200) and the other is refused (403, no longer an admin), and one
    active admin always remains."""
    for i, body in enumerate(({"roles": ["approver"]}, {"status": "inactive"})):
        async with harness() as h:
            a = await h.create_user(f"a{i}@example.com", password=PW)
            b = await h.create_user(f"b{i}@example.com", password=PW)
            ra, rb = await asyncio.gather(h.patch_user(a, b, body), h.patch_user(b, a, body))
            assert sorted([ra.status_code, rb.status_code]) == [200, 403]
        admins = await admin_db.fetchval(
            "SELECT count(*) FROM identity.users WHERE status = 'active' AND 'admin' = ANY(roles)"
        )
        assert admins == 1
        await admin_db.execute("TRUNCATE identity.users CASCADE")


async def test_actor_demoted_in_the_db_is_403(harness: HarnessFactory) -> None:
    """PAY-601, PAY-108: a JWT that still says admin doesn't help once the DB says otherwise."""
    async with harness() as h:
        a = await h.create_user("a@example.com", password=PW)
        b = await h.create_user("b@example.com", password=PW)
        c = await h.create_user("c@example.com", password=PW, roles=("approver",))
        assert (await h.patch_user(a, b, {"roles": ["approver"]})).status_code == 200
        r = await h.patch_user(b, c, {"roles": ["admin"]})
        assert (r.status_code, r.json()["error"]["code"]) == (403, "forbidden_role")


async def test_last_admin_rule_when_the_target_is_the_only_other_admin(
    harness: HarnessFactory,
) -> None:
    """PAY-108: demoting one of two admins is fine; the remaining admin can't touch itself
    (cannot_modify_self), so the company keeps one active admin. Demoting a pending admin is
    allowed (pending admins don't count)."""
    async with harness() as h:
        a = await h.create_user("a@example.com", password=PW)
        b = await h.create_user("b@example.com", password=PW)
        p = await h.create_user("p@example.com", status="pending")
        assert (await h.patch_user(a, b, {"status": "inactive"})).status_code == 200
        r = await h.patch_user(a, a, {"roles": ["approver"]})
        assert r.json()["error"]["code"] == "cannot_modify_self"
        assert (await h.patch_user(a, p, {"roles": ["approver"]})).status_code == 200


async def test_roles_version_unchanged_by_invite_resend_and_accept(
    harness: HarnessFactory,
) -> None:
    """PAY-107: invite, resend and accept leave roles_version at 1 (§6.5)."""
    async with harness() as h:
        admin = await h.create_user("admin@example.com", password=PW)
        r = await h.client.post(
            "/v1/users/invite",
            json={"email": "p@example.com", "name": "P", "roles": ["approver"]},
            headers=h.admin_headers(admin),
        )
        assert r.json()["roles_version"] == 1
        r = await h.client.post(
            f"/v1/invites/{r.json()['pending_invite_id']}/resend",
            headers=h.admin_headers(admin),
        )
        assert r.status_code == 200
        await h.rt.relay.run_once()
        assert h.sender is not None
        accepted = await h.accept(token_in(h.sender.messages[-1]), passphrase())
        assert (accepted.status_code, accepted.json()["roles_version"]) == (200, 1)
