"""The C30 401/403 rule through FastAPI dependencies (platform LLD §3.5; PAY-601, PAY-411)."""

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Annotated, cast

import httpx
import pytest
from fastapi import APIRouter, Depends, Request
from fastapi.testclient import TestClient
from pycommon.app import create_app
from pycommon.auth import Principal, authenticate, require
from pycommon.testing import JsonLogCapture, TokenFactory

from tests.helpers import COMPANY_ID, make_settings

USER = "0193a000-0000-7000-8000-0000000000aa"
AUD = "testsvc"


def _router() -> APIRouter:
    r = APIRouter()
    any_of = require(roles=["payroll_admin", "approver"])
    all_of = require(roles=["payroll_admin", "approver"], any_role=False)
    internal = require(issuers=("payout-worker",), subjects=("service:payout-worker",))
    public_flow = require(subjects=("service:web",))

    @r.get("/user")
    async def user(p: Annotated[Principal, Depends(any_of)]) -> dict[str, str]:
        return {"caller": p.caller_key}

    @r.get("/all-roles")
    async def all_roles(p: Annotated[Principal, Depends(all_of)]) -> dict[str, str]:
        return {"sub": p.sub}

    @r.get("/v1/internal/x")
    async def internal_route(p: Annotated[Principal, Depends(internal)]) -> dict[str, str]:
        return {"sub": p.sub}

    @r.get("/public-flow")
    async def public(p: Annotated[Principal, Depends(public_flow)]) -> dict[str, str]:
        return {"sub": p.sub}

    @r.get("/whoami")
    async def whoami(request: Request) -> dict[str, str]:
        return {"sub": request.state.principal.sub}

    return r


@pytest.fixture
def tf() -> TokenFactory:
    return TokenFactory.create()


@pytest.fixture
def client(tf: TokenFactory) -> Iterator[TestClient]:
    settings = make_settings(internal_jwt_public_keys=tf.keyset)
    app = create_app(settings, routers=[_router()], use_db=False, use_redis=False, instrument=False)
    with TestClient(app) as c:
        yield c


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _user(tf: TokenFactory, roles: list[str], **kw: str) -> str:
    return tf.mint(aud=AUD, sub=USER, cid=COMPANY_ID, roles=roles, **kw)  # type: ignore[arg-type]


def _web_service(tf: TokenFactory) -> str:
    return tf.mint(aud=AUD, sub="service:web", cid=COMPANY_ID)


def _worker(tf: TokenFactory) -> str:
    return tf.mint(issuer="payout-worker", aud=AUD, sub="service:payout-worker", cid=COMPANY_ID)


def _code(resp: httpx.Response) -> tuple[int, str]:
    return resp.status_code, resp.json()["error"]["code"]


def test_valid_user_with_role_passes(client: TestClient, tf: TokenFactory) -> None:
    """PAY-601: a web user token with one of the roles reaches the handler; caller_key set."""
    resp = client.get("/user", headers=_bearer(_user(tf, ["approver"])))
    assert resp.status_code == 200
    assert resp.json() == {"caller": f"web:{USER}"}
    assert client.get("/whoami", headers=_bearer(_user(tf, []))).json() == {"sub": USER}


def test_missing_and_non_bearer_headers_are_401(client: TestClient, tf: TokenFactory) -> None:
    """PAY-600: no Authorization header or a non-Bearer one → 401 invalid_token."""
    assert _code(client.get("/user")) == (401, "invalid_token")
    token = _user(tf, ["approver"])
    assert _code(client.get("/user", headers={"Authorization": f"Basic {token}"})) == (
        401,
        "invalid_token",
    )
    assert _code(client.get("/user", headers={"Authorization": "Bearer "})) == (
        401,
        "invalid_token",
    )


def test_web_token_on_internal_route_is_401(
    client: TestClient, tf: TokenFactory, caplog_json: JsonLogCapture
) -> None:
    """PAY-411 / C30: a web token on /v1/internal/* → 401 invalid_token (issuer_not_allowed)."""
    assert _code(client.get("/v1/internal/x", headers=_bearer(_user(tf, ["admin"])))) == (
        401,
        "invalid_token",
    )
    events = caplog_json.events("auth.token.rejected")
    assert [(e["reason"], e["iss"]) for e in events] == [("issuer_not_allowed", "web")]


def test_worker_token_on_user_route_is_401(client: TestClient, tf: TokenFactory) -> None:
    """PAY-411 / C30: a worker token on a user route → 401 (the issuer check runs first)."""
    assert _code(client.get("/user", headers=_bearer(_worker(tf)))) == (401, "invalid_token")
    assert client.get("/v1/internal/x", headers=_bearer(_worker(tf))).status_code == 200


def test_service_web_on_user_route_is_403(
    client: TestClient, tf: TokenFactory, caplog_json: JsonLogCapture
) -> None:
    """C30: sub=service:web on a user route → 403 forbidden_role {route, subject_kind}."""
    assert _code(client.get("/user", headers=_bearer(_web_service(tf)))) == (403, "forbidden_role")
    (event,) = caplog_json.events("auth.forbidden")
    assert (event["route"], event["subject_kind"], event["level"]) == (
        "/user",
        "service:web",
        "info",
    )


def test_user_token_on_public_flow_route_is_403(client: TestClient, tf: TokenFactory) -> None:
    """C30: a user token on a service:web-only route → 403; service:web passes."""
    assert _code(client.get("/public-flow", headers=_bearer(_user(tf, ["admin"])))) == (
        403,
        "forbidden_role",
    )
    assert client.get("/public-flow", headers=_bearer(_web_service(tf))).json() == {
        "sub": "service:web"
    }


def test_missing_role_and_any_vs_all(
    client: TestClient, tf: TokenFactory, caplog_json: JsonLogCapture
) -> None:
    """PAY-601: no listed role → 403; any_role=False needs every role."""
    assert _code(client.get("/user", headers=_bearer(_user(tf, ["admin"])))) == (
        403,
        "forbidden_role",
    )
    (event,) = caplog_json.events("auth.forbidden")
    assert event["missing_roles"] == ["approver", "payroll_admin"]
    assert _code(client.get("/all-roles", headers=_bearer(_user(tf, ["approver"])))) == (
        403,
        "forbidden_role",
    )
    assert caplog_json.events("auth.forbidden")[-1]["missing_roles"] == ["payroll_admin"]
    both = _bearer(_user(tf, ["approver", "payroll_admin"]))
    assert client.get("/all-roles", headers=both).status_code == 200


def test_logs_carry_no_token_name_or_sid(
    client: TestClient, tf: TokenFactory, caplog_json: JsonLogCapture
) -> None:
    """PAY-NFR-07: auth logs and request logs never hold the token, name or sid."""
    token = _user(tf, ["admin"], name="Secret Name", sid="sid-secret-1")
    client.get("/user", headers=_bearer(token))
    client.get("/v1/internal/x", headers=_bearer(token))
    text = caplog_json.text
    assert caplog_json.events("auth.forbidden")
    assert token not in text
    assert "Secret Name" not in text
    assert "sid-secret-1" not in text


async def test_authenticate_needs_a_verifier() -> None:
    """PAY-600: authenticate refuses to run without a configured verifier."""
    request = cast(
        Request,
        SimpleNamespace(state=SimpleNamespace(), app=SimpleNamespace(state=SimpleNamespace())),
    )
    with pytest.raises(RuntimeError, match="verifier"):
        await authenticate(request)
