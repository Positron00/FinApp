"""``GET /v1/users/me`` (identity LLD §3.4; PAY-107, R19) and the per-caller limit (C17)."""

from __future__ import annotations

from uuid import UUID

from tests.helpers import COMPANY_ID
from tests.integration.conftest import HarnessFactory


async def test_me_returns_me_out_whatever_the_status(harness: HarnessFactory) -> None:
    """PAY-107, R19: MeOut with status, roles_version, name and company_id, also when inactive;
    a pending user's pending_invite_id is set."""
    async with harness() as h:
        active = await h.create_user("a@example.com", password="x" * 12, roles=("approver",))
        gone = await h.create_user("g@example.com", status="inactive", name="Gone")
        r = await h.client.get("/v1/users/me", headers=h.user_headers(active))
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == str(active)
        assert body["company_id"] == str(COMPANY_ID)
        assert (body["status"], body["roles"], body["roles_version"]) == ("active", ["approver"], 1)
        assert body["name"] == "Test User"
        assert body["pending_invite_id"] is None
        assert body["activated_at"] is not None
        r = await h.client.get("/v1/users/me", headers=h.user_headers(gone))
        assert (r.json()["status"], r.json()["name"]) == ("inactive", "Gone")

        invited = await h.client.post(
            "/v1/users/invite",
            json={"email": "p@example.com", "name": "P", "roles": ["approver"]},
            headers=h.user_headers(await h.create_user("adm@example.com")),
        )
        pid = UUID(invited.json()["id"])
        r = await h.client.get("/v1/users/me", headers=h.user_headers(pid))
        assert r.json()["pending_invite_id"] == invited.json()["pending_invite_id"]


async def test_me_unknown_user_and_wrong_caller(harness: HarnessFactory) -> None:
    """PAY-602, PAY-601: an unknown user → 404 user_not_found; service:web → 403."""
    async with harness() as h:
        r = await h.client.get(
            "/v1/users/me", headers=h.user_headers(UUID("0193a000-0000-7000-8000-0000000000ff"))
        )
        assert (r.status_code, r.json()["error"]["code"]) == (404, "user_not_found")
        r = await h.client.get("/v1/users/me", headers=h.web_headers())
        assert (r.status_code, r.json()["error"]["code"]) == (403, "forbidden_role")


async def test_caller_limit_for_user_tokens(harness: HarnessFactory) -> None:
    """PAY-603, C17: user tokens get SERVICE_RATE_LIMIT_USER per minute → 429 + Retry-After."""
    async with harness(service_rate_limit_user=2) as h:
        uid = await h.create_user("a@example.com")
        codes = [
            (await h.client.get("/v1/users/me", headers=h.user_headers(uid))).status_code
            for _ in range(3)
        ]
        assert codes == [200, 200, 429]
        r = await h.client.get("/v1/users/me", headers=h.user_headers(uid))
        assert 1 <= int(r.headers["retry-after"]) <= 60
