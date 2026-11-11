"""Login limits and lockout with the production values (identity LLD §6.4, §7.2; R15)."""

from __future__ import annotations

import asyncio
from typing import Any

import redis.asyncio as aioredis
from identity.domain.ip import email_key
from identity.limits import AuthLimits
from pycommon.testing import FakeClock, OtelMemory

from tests.helpers import AUDIT_KEY, PRODUCTION_ARGON2
from tests.integration.conftest import Harness, HarnessFactory, token_in

PW = "correct horse battery staple"
BAD = "wrong password!!"
NEW_PW = "a brand new passphrase"
PRODUCTION_LIMITS = {
    "auth_login_limit_account": 5,
    "auth_login_limit_ip": 20,
    "identity_login_window_s": 900,
    "identity_lockout_base_s": 60,
    "identity_lockout_max_s": 3600,
    "identity_lockout_level_ttl_s": 86400,
}


def ek(email: str) -> str:
    return email_key(email, AUDIT_KEY.encode())


async def fail_n(h: Harness, email: str, n: int, ip: str) -> None:
    for i in range(n):
        r = await h.login(email, BAD, ip=ip)
        assert r.status_code == 401, (i, r.text)


async def lockouts(admin_db: Any) -> list[Any]:
    rows: list[Any] = await admin_db.fetch(
        "SELECT target_user_id, details FROM identity.audit_log "
        "WHERE event = 'auth.lockout' ORDER BY id"
    )
    return rows


async def test_lockout_production_values(  # noqa: PLR0915  (one scripted scenario)
    harness: HarnessFactory,
    admin_db: Any,
    redis_admin_client: aioredis.Redis,
    otel_memory: OtelMemory,
) -> None:
    """PAY-104, R15: attempts 1-5 → 401, the 6th → 429 Retry-After 60 and auth.lockout (level 1);
    the right password while locked → 429; after the lock, 5 more failures → 401 and the next
    locks for 120 s; success deletes the level; unknown emails lock identically; concurrent 6th
    attempts make one lock and one audit row; successful sign-ins never count toward the account
    limit (C63); the 21st attempt from one IP is 429 (login_ip); no Redis key holds an email or
    IP; auth_signins_total counts success, failure and rate_limited (C19)."""
    clock = FakeClock()  # 2026-11-10T12:00:00Z: the start of a 900 s window
    async with harness(clock=clock, **PRODUCTION_LIMITS, **PRODUCTION_ARGON2) as h:
        uid = await h.create_user("ann@example.com", password=PW)
        await h.create_user("bob@example.com", password=PW)

        # Attempts 1-5 → 401, the 6th → 429 and a level-1 lock of 60 s.
        await fail_n(h, "ann@example.com", 5, ip="198.51.100.1")
        r = await h.login("ann@example.com", BAD, ip="198.51.100.1")
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "rate_limited"
        assert r.headers["retry-after"] == "60"
        [lock] = await lockouts(admin_db)
        assert lock["target_user_id"] == uid
        assert lock["details"] == '{"level": 1, "lock_seconds": 60}'

        # The right password while locked → 429; the lock isn't extended.
        r = await h.login("ann@example.com", PW, ip="198.51.100.1")
        assert r.status_code == 429
        assert 1 <= int(r.headers["retry-after"]) <= 60
        assert len(await lockouts(admin_db)) == 1

        # The lock ends: 5 more failures → 401, the next → a level-2 lock of 120 s.
        await redis_admin_client.delete(AuthLimits.lock_key(ek("ann@example.com")))
        await fail_n(h, "ann@example.com", 5, ip="198.51.100.1")
        r = await h.login("ann@example.com", BAD, ip="198.51.100.1")
        assert r.status_code == 429
        assert r.headers["retry-after"] == "120"
        pttl = await redis_admin_client.pttl(AuthLimits.lock_key(ek("ann@example.com")))
        assert 115_000 < pttl <= 120_000

        # Success after the lock deletes the level key.
        await redis_admin_client.delete(AuthLimits.lock_key(ek("ann@example.com")))
        assert await redis_admin_client.get(AuthLimits.level_key(ek("ann@example.com"))) == b"2"
        assert (await h.login("ann@example.com", PW, ip="198.51.100.1")).status_code == 200
        assert await redis_admin_client.exists(AuthLimits.level_key(ek("ann@example.com"))) == 0

        # Unknown emails lock exactly the same way (no enumeration).
        await fail_n(h, "ghost@example.com", 5, ip="198.51.100.2")
        r = await h.login("ghost@example.com", BAD, ip="198.51.100.2")
        assert r.status_code == 429
        assert r.headers["retry-after"] == "60"
        ghost = (await lockouts(admin_db))[-1]
        assert ghost["target_user_id"] is None

        # Two concurrent 6th attempts → one lock, one audit row.
        await fail_n(h, "bob@example.com", 5, ip="198.51.100.3")
        before = len(await lockouts(admin_db))
        a, b = await asyncio.gather(
            h.login("bob@example.com", BAD, ip="198.51.100.3"),
            h.login("bob@example.com", BAD, ip="198.51.100.3"),
        )
        assert (a.status_code, b.status_code) == (429, 429)
        assert len(await lockouts(admin_db)) == before + 1

        # Only failed attempts count toward the account limit (C63): ten sign-ins in a row are
        # fine, and five failures after them are still 401.
        await h.create_user("carl@example.com", password=PW)
        for _ in range(10):
            assert (await h.login("carl@example.com", PW, ip="198.51.100.4")).status_code == 200
        await fail_n(h, "carl@example.com", 5, ip="198.51.100.5")
        assert (await h.login("carl@example.com", PW, ip="198.51.100.5")).status_code == 429

        # 21 attempts from one IP over different emails → the 21st is 429 (login_ip).
        for i in range(20):
            r = await h.login(f"user{i}@example.com", BAD, ip="198.51.100.9")
            assert r.status_code == 401
        r = await h.login("user99@example.com", BAD, ip="198.51.100.9")
        assert r.status_code == 429
        assert int(r.headers["retry-after"]) == 900

    keys = [k.decode() if isinstance(k, bytes) else k for k in await redis_admin_client.keys("*")]
    assert keys
    assert all(k.startswith("identity:rl:") for k in keys)
    joined = " ".join(keys)
    for needle in ("example.com", "ann", "ghost", "198.51.100"):
        assert needle not in joined
    limited = {a["scope"]: v for a, v in otel_memory.metric_points("rate_limited")}
    assert limited["login_locked"] >= 1
    assert limited["login_account"] >= 3
    assert limited["login_ip"] == 1
    signins = {a["outcome"]: v for a, v in otel_memory.metric_points("auth_signins")}
    assert signins["success"] == 11  # ann once, carl ten times
    assert signins["rate_limited"] >= 8
    assert signins["failure"] >= 45


async def lock_out(h: Harness, email: str, ip: str) -> None:
    """Five failures and a sixth attempt from someone else's IP: the account is locked."""
    await fail_n(h, email, 5, ip=ip)
    r = await h.login(email, BAD, ip=ip)
    assert (r.status_code, int(r.headers["retry-after"])) == (429, 60)


async def test_reset_confirm_clears_the_lockout(
    harness: HarnessFactory, redis_admin_client: aioredis.Redis
) -> None:
    """PAY-104, PAY-105, C74: an attacker who locks an account (from any IP) can't keep its
    owner out: a confirmed reset deletes the lock, its level and the level-0 failure count, so
    the owner signs in at once with the new password, and the next lock starts at level 1."""
    async with harness(clock=FakeClock(), **PRODUCTION_LIMITS) as h:
        await h.create_user("ann@example.com", password=PW)
        await lock_out(h, "ann@example.com", ip="203.0.113.66")
        assert (await h.login("ann@example.com", PW)).status_code == 429  # R15: locked
        assert (await h.reset_request("ann@example.com")).status_code == 202
        assert h.sender is not None
        await h.rt.relay.run_once()
        token = token_in(h.sender.messages[-1])
        assert (await h.reset_confirm(token, NEW_PW)).status_code == 200
        for key in (
            AuthLimits.lock_key(ek("ann@example.com")),
            AuthLimits.level_key(ek("ann@example.com")),
        ):
            assert await redis_admin_client.exists(key) == 0
        assert (await h.login("ann@example.com", NEW_PW)).status_code == 200
        # The attacker starts over: five more 401s, then a level-1 (60 s) lock again.
        await lock_out(h, "ann@example.com", ip="203.0.113.66")


async def test_invite_accept_clears_a_lockout_on_the_pending_email(harness: HarnessFactory) -> None:
    """PAY-103, PAY-104, C74: failed logins against an invited (pending) email lock it like any
    other; accepting the invite clears the lock, so the new user can sign in at once."""
    async with harness(clock=FakeClock(), **PRODUCTION_LIMITS) as h:
        admin = await h.create_user("admin@example.com", password=PW)
        r = await h.client.post(
            "/v1/users/invite",
            json={"email": "bea@example.com", "name": "Bea", "roles": ["approver"]},
            headers=h.user_headers(admin, ("admin",)),
        )
        assert r.status_code == 201, r.text
        await lock_out(h, "bea@example.com", ip="203.0.113.66")
        assert h.sender is not None
        await h.rt.relay.run_once()
        assert (await h.accept(token_in(h.sender.messages[-1]), NEW_PW)).status_code == 200
        assert (await h.login("bea@example.com", NEW_PW)).status_code == 200
