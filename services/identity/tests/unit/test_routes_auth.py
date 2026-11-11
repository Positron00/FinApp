"""Route authorization matrix without I/O (identity LLD §3.1-3.2, §7.1; C30).

Every case here is decided before the handler touches Postgres or Redis; the 2xx cells and the
admin DB re-check are covered by the integration suites.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from identity.main import build_app
from pycommon.auth import TokenSigner
from pycommon.testing import FakeClock, TokenFactory

from tests.helpers import COMPANY_ID, make_settings, web_only_keyset

USER = "0193a000-0000-7000-8000-0000000000aa"
ROUTES = [
    ("POST", "/v1/auth/login", "public"),
    ("POST", "/v1/auth/password-reset/request", "public"),
    ("POST", "/v1/auth/password-reset/confirm", "public"),
    ("POST", "/v1/invites/accept", "public"),
    ("GET", "/v1/users/me", "user"),
    ("POST", "/v1/users/invite", "user"),
    ("GET", "/v1/users", "user"),
    ("PATCH", f"/v1/users/{USER}", "user"),
    ("POST", f"/v1/invites/{USER}/resend", "user"),
    ("GET", "/v1/audit-log", "user"),
]
BODY = {"email": "a@b.example", "password": "x" * 12, "token": "t", "name": "A", "roles": ["admin"]}
BODIES: dict[str, dict[str, object]] = {
    "/v1/auth/login": {"email": BODY["email"], "password": BODY["password"]},
    "/v1/auth/password-reset/request": {"email": BODY["email"]},
    "/v1/auth/password-reset/confirm": {"token": BODY["token"], "password": BODY["password"]},
    "/v1/invites/accept": {"token": BODY["token"], "password": BODY["password"]},
    "/v1/users/invite": {"email": BODY["email"], "name": BODY["name"], "roles": BODY["roles"]},
    f"/v1/users/{USER}": {"status": "inactive"},
}


@pytest.fixture
def client(token_factory: TokenFactory) -> Iterator[TestClient]:
    s = make_settings(internal_jwt_public_keys=web_only_keyset(token_factory))
    app = build_app(s, instrument=False, run_relay=False)
    with TestClient(app, raise_server_exceptions=False) as c:
        app.state.ctx.limiter = None  # the per-caller limit needs Redis (integration)
        yield c


def _body(path: str) -> dict[str, object] | None:
    return BODIES.get(path)


def _call(c: TestClient, method: str, path: str, token: str | None) -> tuple[int, str]:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    r = c.request(method, path, json=_body(path), headers=headers)
    return r.status_code, r.json()["error"]["code"]


def _tokens(tf: TokenFactory) -> dict[str, str]:
    web = tf.signers["web"]
    past = FakeClock()
    past.advance(-3600)
    expired = TokenSigner(tf.private_pems["web"], kid=tf.kids["web"], issuer="web", clock=past)
    mismatch = TokenSigner(tf.private_pems["web"], kid=tf.kids["web"], issuer="payout-worker")
    return {
        "expired": expired.mint(aud="identity", sub=USER, cid=COMPANY_ID),
        "wrong_aud": web.mint(aud="payroll", sub=USER, cid=COMPANY_ID),
        "worker_kid": tf.mint(
            issuer="payout-worker", aud="identity", sub="service:payout-worker", cid=COMPANY_ID
        ),
        "issuer_mismatch": mismatch.mint(aud="identity", sub=USER, cid=COMPANY_ID),
        "garbage": "not.a.jwt",
    }


@pytest.mark.parametrize(("method", "path", "kind"), ROUTES)
def test_token_failures_are_401(
    client: TestClient, token_factory: TokenFactory, method: str, path: str, kind: str
) -> None:
    """PAY-600, C30: no token, expired, wrong aud, unknown (worker) kid, issuer/kid mismatch."""
    assert _call(client, method, path, None) == (401, "invalid_token")
    for token in _tokens(token_factory).values():
        assert _call(client, method, path, token) == (401, "invalid_token")


@pytest.mark.parametrize(("method", "path", "kind"), ROUTES)
def test_wrong_kind_of_token_is_403(
    client: TestClient, token_factory: TokenFactory, method: str, path: str, kind: str
) -> None:
    """PAY-601, C30: service:web only on the public routes; user tokens only elsewhere."""
    if kind == "public":
        token = token_factory.mint(aud="identity", sub=USER, cid=COMPANY_ID, roles=["admin"])
    else:
        token = token_factory.mint(aud="identity", sub="service:web", cid=COMPANY_ID)
    assert _call(client, method, path, token) == (403, "forbidden_role")


def test_unparseable_subject_is_403(client: TestClient, token_factory: TokenFactory) -> None:
    """PAY-601: a subject that is neither a UUID nor service:web is the wrong kind."""
    token = token_factory.mint(aud="identity", sub="someone", cid=COMPANY_ID)
    assert _call(client, "GET", "/v1/users/me", token) == (403, "forbidden_role")


def test_validation_details_never_echo_input(
    client: TestClient, token_factory: TokenFactory
) -> None:
    """PAY-602, PAY-NFR-07, C14, C56: details are [{field, issue}] and never the value."""
    token = token_factory.mint(aud="identity", sub="service:web", cid=COMPANY_ID)
    secret = "p" * 300
    r = client.post(
        "/v1/auth/login",
        json={"email": "nobody@example.com", "password": secret},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "validation_error"
    assert body["details"] == [{"field": "body.password", "issue": "string_too_long"}]
    assert secret not in r.text
    assert r.headers["cache-control"] == "no-store"


def test_bad_client_ip_header_is_422(client: TestClient, token_factory: TokenFactory) -> None:
    """PAY-603, C16: an X-Client-IP that isn't an IP address is 422 header.X-Client-IP."""
    token = token_factory.mint(aud="identity", sub="service:web", cid=COMPANY_ID)
    r = client.post(
        "/v1/invites/accept",
        json={"token": "t", "password": "x" * 12},
        headers={"Authorization": f"Bearer {token}", "X-Client-IP": "999.1.1.1"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["details"] == [{"field": "header.X-Client-IP", "issue": "ip_address"}]
    assert "999.1.1.1" not in r.text


def test_body_over_256_kb_is_413(client: TestClient, token_factory: TokenFactory) -> None:
    """PAY-604: bodies over MAX_BODY_BYTES are 413 payload_too_large."""
    token = token_factory.mint(aud="identity", sub="service:web", cid=COMPANY_ID)
    r = client.post(
        "/v1/auth/login",
        content=b"{" + b" " * 300_000 + b"}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "payload_too_large"


def test_livez(client: TestClient) -> None:
    """PAY-605: /livez needs no token and no dependency."""
    r = client.get("/livez")
    assert r.status_code == 200


@pytest.mark.parametrize("path", ["/v1/users/invite", "/v1/users/me"])
def test_patch_on_literal_user_paths_is_405(
    client: TestClient, token_factory: TokenFactory, path: str
) -> None:
    """PAY-602, C65: PATCH /v1/users/{id} only matches a UUID, so a literal sibling path answers
    405 with a complete Allow header rather than 422 (Schemathesis ST204)."""
    token = token_factory.mint(
        aud="identity", sub=str(uuid.uuid4()), cid=COMPANY_ID, roles=["admin"], name="Ann"
    )
    r = client.patch(path, json={}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 405
    assert r.json()["error"]["code"] == "method_not_allowed"
    assert "PATCH" not in r.headers["allow"]
