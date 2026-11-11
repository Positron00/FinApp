"""The assembled app without I/O: route guards, the role matrix, 413 and health (PAY-600, 601,
604, 605, PAY-411; design §8, C30)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from payroll.api.deps import require_web_user, require_worker
from payroll.main import build_app, migrations_dir
from pycommon.testing import TokenFactory

from tests.helpers import COMPANY_ID, make_settings

RID, PID = uuid4(), uuid4()


@dataclass
class Harness:
    app: FastAPI
    client: httpx.AsyncClient
    tokens: TokenFactory

    def bearer(
        self,
        *,
        roles: tuple[str, ...] = (),
        sub: str | None = None,
        issuer: str = "web",
    ) -> dict[str, str]:
        token = self.tokens.mint(
            issuer=issuer,  # type: ignore[arg-type]
            aud="payroll",
            sub=sub or str(uuid4()),
            cid=COMPANY_ID,
            roles=roles,
        )
        return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def harness(token_factory: TokenFactory) -> AsyncIterator[Harness]:
    settings = make_settings(internal_jwt_public_keys=token_factory.keyset_json)
    app = build_app(settings, instrument=False, relay=False)
    async with app.router.lifespan_context(app):
        app.state.ctx.limiter = None  # no Redis in unit tests: the caller limit is skipped
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://payroll") as client:
            yield Harness(app, client, token_factory)


def _api_routes(app: FastAPI) -> list[APIRoute]:
    """Every APIRoute, including those of included routers (FastAPI wraps them)."""
    out: list[APIRoute] = []
    stack: list[Any] = list(app.routes)
    while stack:
        r = stack.pop()
        if isinstance(r, APIRoute):
            out.append(r)
        elif hasattr(r, "original_router"):
            stack.extend(r.original_router.routes)
    return out


def _dependency_calls(route: APIRoute) -> set[Callable[..., Any]]:
    seen: set[Callable[..., Any]] = set()
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        if dep.call is not None:
            seen.add(dep.call)
        stack.extend(dep.dependencies)
    return seen


async def test_every_route_has_a_guard(harness: Harness) -> None:
    """PAY-601: every non-health route depends on require_web_user or require_worker."""
    routes = [r for r in _api_routes(harness.app) if r.path not in ("/livez", "/readyz")]
    assert len(routes) >= 12
    for route in routes:
        calls = _dependency_calls(route)
        guard = require_worker if route.path.startswith("/v1/internal/") else require_web_user
        assert guard in calls, route.path


ANY = ("admin", "payroll_admin", "approver")
ROUTES: list[tuple[str, str, tuple[str, ...]]] = [
    ("GET", "/v1/company/settings", ANY),
    ("PUT", "/v1/company/settings", ("admin",)),
    ("GET", "/v1/employees", ANY),
    ("POST", "/v1/employees", ("payroll_admin",)),
    ("GET", f"/v1/employees/{PID}", ANY),
    ("PATCH", f"/v1/employees/{PID}", ("payroll_admin",)),
    ("GET", "/v1/audit-events", ("admin",)),
    ("GET", "/v1/pay-runs", ANY),
    ("POST", "/v1/pay-runs", ("payroll_admin",)),
    ("GET", f"/v1/pay-runs/{RID}", ANY),
    ("POST", f"/v1/pay-runs/{RID}/calculate", ("payroll_admin",)),
    ("POST", f"/v1/pay-runs/{RID}/approve", ("approver",)),
    ("GET", f"/v1/pay-runs/{RID}/payslips", ANY),
    ("GET", f"/v1/pay-runs/{RID}/payslips/{PID}", ANY),
    ("GET", f"/v1/pay-runs/{RID}/payouts", ANY),
]
INTERNAL = [
    ("GET", f"/v1/internal/payouts/{PID}/instruction?attempt=1"),
    ("POST", f"/v1/internal/payouts/{PID}/result"),
]


def _code(r: httpx.Response) -> tuple[int, str | None]:
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    return r.status_code, (body.get("error") or {}).get("code")


@pytest.mark.parametrize(
    ("method", "path", "allowed"), ROUTES, ids=[f"{m} {p}" for m, p, _ in ROUTES]
)
async def test_role_matrix(
    harness: Harness, method: str, path: str, allowed: tuple[str, ...]
) -> None:
    """PAY-601, C30: wrong role/no role → 403 forbidden_role; allowed roles pass the guard."""
    for role in (*ANY, "unknown_role"):
        r = await harness.client.request(method, path, headers=harness.bearer(roles=(role,)))
        if role in allowed:
            assert r.status_code not in (401, 403), (role, _code(r))
        else:
            assert _code(r) == (403, "forbidden_role"), role
    r = await harness.client.request(method, path, headers=harness.bearer(roles=()))
    assert _code(r) == (403, "forbidden_role")
    r = await harness.client.request(
        method, path, headers=harness.bearer(roles=ANY, sub="service:web")
    )
    assert _code(r) == (403, "forbidden_role")  # a valid token of the wrong kind (C30)
    r = await harness.client.request(
        method, path, headers=harness.bearer(issuer="payout-worker", sub="service:payout-worker")
    )
    assert _code(r) == (401, "invalid_token")  # a worker token on a public route (R34)


@pytest.mark.parametrize(("method", "path"), INTERNAL)
async def test_internal_routes_are_worker_only(harness: Harness, method: str, path: str) -> None:
    """PAY-411, C30: web tokens → 401; worker-issued with another sub → 403; the worker passes."""
    r = await harness.client.request(method, path, headers=harness.bearer(roles=ANY))
    assert _code(r) == (401, "invalid_token")
    r = await harness.client.request(
        method, path, headers=harness.bearer(issuer="payout-worker", sub="service:web")
    )
    assert _code(r) == (403, "forbidden_role")
    r = await harness.client.request(
        method,
        path,
        headers=harness.bearer(issuer="payout-worker", sub="service:payout-worker"),
        json={"attempt": 1, "outcome": "indeterminate"},
    )
    assert r.status_code not in (401, 403)


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic abc"}],
)
async def test_missing_or_malformed_token_is_401(harness: Harness, headers: dict[str, str]) -> None:
    """PAY-600: a missing or malformed token is 401 invalid_token on every route class."""
    for method, path, _ in ROUTES[:3]:
        r = await harness.client.request(method, path, headers=headers)
        assert _code(r) == (401, "invalid_token")


async def test_issuer_kid_mismatch_and_other_key_are_401(harness: Harness) -> None:
    """PAY-600: a token signed by an unknown key is 401 (bad signature / unknown kid)."""
    stranger = TokenFactory.create()
    token = stranger.mint(aud="payroll", sub=str(uuid4()), cid=COMPANY_ID, roles=ANY)
    r = await harness.client.get("/v1/employees", headers={"Authorization": f"Bearer {token}"})
    assert _code(r) == (401, "invalid_token")


async def test_payload_too_large_is_413(harness: Harness) -> None:
    """PAY-604: a body over MAX_BODY_BYTES (262,144) is 413 payload_too_large."""
    r = await harness.client.post(
        "/v1/employees",
        content=b"{" + b" " * 262_145 + b"}",
        headers={**harness.bearer(roles=("payroll_admin",)), "content-type": "application/json"},
    )
    assert _code(r) == (413, "payload_too_large")


async def test_livez_needs_no_token(harness: Harness) -> None:
    """PAY-605: /livez answers 200 without a JWT; /readyz reports its checks."""
    assert (await harness.client.get("/livez")).status_code == 200
    r = await harness.client.get("/readyz")
    assert set(r.json()["checks"]) == {"db", "migrations", "redis"}


async def test_approve_key_rules(harness: Harness) -> None:
    """PAY-400, C34: no Idempotency-Key → 400 idempotency_key_required; malformed → 422."""
    headers = harness.bearer(roles=("approver",))
    r = await harness.client.post(f"/v1/pay-runs/{RID}/approve", headers=headers)
    assert _code(r) == (400, "idempotency_key_required")
    r = await harness.client.post(
        f"/v1/pay-runs/{RID}/approve", headers={**headers, "Idempotency-Key": "bad key!"}
    )
    assert _code(r) == (422, "validation_error")
    assert r.json()["error"]["details"] == [
        {"field": "header.Idempotency-Key", "issue": "string_pattern_mismatch"}
    ]


def test_migrations_dir_is_found() -> None:
    """PAY-605: /readyz compares against the head of services/payroll/migrations."""
    assert migrations_dir().endswith("migrations")
