"""``POST /v1/auth/login`` (identity LLD §6.3-6.4, §7.2)."""

from __future__ import annotations

import statistics
import time
from typing import Any

from identity.domain.ip import ip_hash
from pycommon.testing import OtelMemory

from tests.helpers import AUDIT_KEY, COMPANY_ID, PRODUCTION_ARGON2
from tests.integration.conftest import CLIENT_IP, HarnessFactory

PW = "correct horse battery staple"
RAISED = {"auth_login_limit_account": 1000, "auth_login_limit_ip": 1000}


async def test_success_returns_session_user(harness: HarnessFactory, admin_db: Any) -> None:
    """PAY-104, PAY-106, C6: 200 SessionUser; audit auth.signin.succeeded with ip_hash."""
    async with harness() as h:
        uid = await h.create_user("Ann@Example.com", password=PW, roles=("approver", "admin"))
        r = await h.login("ann@example.COM", PW)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert r.json() == {
        "user_id": str(uid),
        "company_id": str(COMPANY_ID),
        "email": "Ann@Example.com",
        "name": "Test User",
        "roles": ["admin", "approver"],
        "roles_version": 1,
        "status": "active",
    }
    row = await admin_db.fetchrow("SELECT * FROM identity.audit_log")
    assert row["event"] == "auth.signin.succeeded"
    assert row["actor_user_id"] == uid
    assert row["target_user_id"] == uid
    assert row["ip_hash"] == ip_hash(CLIENT_IP, AUDIT_KEY.encode())


async def test_every_failure_has_the_same_401(
    harness: HarnessFactory, admin_db: Any, otel_memory: OtelMemory
) -> None:
    """PAY-104: wrong password, unknown email, pending and inactive users get the identical 401
    body; argon2 verify runs on every path; one audit row each, with the reason."""
    async with harness() as h:
        await h.create_user("ann@example.com", password=PW)
        await h.create_user("pending@example.com", status="pending")
        await h.create_user("gone@example.com", password=PW, status="inactive")
        bodies = []
        for email, pw in [
            ("ann@example.com", "wrong password!!"),
            ("nobody@example.com", PW),
            ("pending@example.com", PW),
            ("gone@example.com", PW),
        ]:
            r = await h.login(email, pw, ip=None)
            assert r.status_code == 401
            body = r.json()["error"]
            bodies.append((body["code"], body["message"], tuple(sorted(body))))
    assert len(set(bodies)) == 1
    assert bodies[0][0] == "invalid_credentials"
    rows = await admin_db.fetch(
        "SELECT event, details->>'reason' AS reason, ip_hash FROM identity.audit_log ORDER BY id"
    )
    assert [(r["event"], r["reason"]) for r in rows] == [
        ("auth.signin.failed", "bad_password"),
        ("auth.signin.failed", "unknown_user"),
        ("auth.signin.failed", "not_active"),
        ("auth.signin.failed", "not_active"),
    ]
    assert all(r["ip_hash"] is None for r in rows)  # no X-Client-IP → NULL (C16)
    verifies = [
        p for a, p in otel_memory.metric_points("identity_password_hash_seconds")
        if a == {"op": "verify"}
    ]  # fmt: skip
    assert verifies[0].count == 4
    failures = [
        v for a, v in otel_memory.metric_points("auth_signins") if a == {"outcome": "failure"}
    ]
    assert failures == [4]


async def test_rehash_after_parameter_change(harness: HarnessFactory, admin_db: Any) -> None:
    """PAY-104 (§6.2): a successful login with old parameters stores a new hash."""
    async with harness() as h:
        await h.create_user("ann@example.com", password=PW)
    old = await admin_db.fetchval("SELECT password_hash FROM identity.credentials")
    async with harness(identity_argon2_time_cost=2) as h:
        assert (await h.login("ann@example.com", PW)).status_code == 200
    new = await admin_db.fetchval("SELECT password_hash FROM identity.credentials")
    assert new != old
    assert ",t=2," in new


async def test_login_timing(harness: HarnessFactory) -> None:
    """PAY-104, C45 (test_login_timing): with production argon2 parameters and raised limits,
    the median latency of 30 unknown-email and 30 wrong-password logins differs by <= 20%."""
    async with harness(**PRODUCTION_ARGON2, **RAISED) as h:
        await h.create_user("ann@example.com", password=PW)
        await h.login("ann@example.com", "warm-up password")  # warm the pool and caches
        unknown: list[float] = []
        wrong: list[float] = []
        for i in range(30):
            for email, sink in (("ann@example.com", wrong), (f"x{i}@example.com", unknown)):
                t0 = time.perf_counter()
                r = await h.login(email, "wrong password!!")
                sink.append(time.perf_counter() - t0)
                assert r.status_code == 401
    m_unknown, m_wrong = statistics.median(unknown), statistics.median(wrong)
    assert abs(m_unknown - m_wrong) / max(m_unknown, m_wrong) <= 0.20, (m_unknown, m_wrong)


async def test_service_web_only_and_no_caller_limit(harness: HarnessFactory) -> None:
    """PAY-601, R18, C17: a user token is 403 on login; service:web is never caller-limited."""
    async with harness(service_rate_limit_user=1, **RAISED) as h:
        uid = await h.create_user("ann@example.com", password=PW)
        r = await h.client.post(
            "/v1/auth/login",
            json={"email": "ann@example.com", "password": PW},
            headers=h.user_headers(uid),
        )
        assert r.status_code == 403
        for _ in range(3):
            assert (await h.login("ann@example.com", PW)).status_code == 200
