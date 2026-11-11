"""Error codes, request models and cursors (identity LLD §3.3, §3.5; PAY-602, PAY-108)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from identity.api import errors as api_errors
from identity.api.pagination import decode_cursor, encode_cursor
from identity.api.schemas import PatchUserIn, ResetConfirmIn, ResetRequestIn
from pycommon.errors import AppError, ValidationFailed, install_handlers
from pydantic import ValidationError

# identity LLD §3.5: code → status.
EXPECTED = {
    "InvalidCredentials": ("invalid_credentials", 401),
    "InviteInvalid": ("invite_invalid", 400),
    "ResetInvalid": ("reset_invalid", 400),
    "PasswordTooWeak": ("password_too_weak", 422),
    "UserEmailTaken": ("user_email_taken", 409),
    "InviteNotPending": ("invite_not_pending", 409),
    "InviteNotFound": ("invite_not_found", 404),
    "UserNotFound": ("user_not_found", 404),
    "CannotModifySelf": ("cannot_modify_self", 409),
    "LastAdmin": ("last_admin", 409),
}


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    install_handlers(app)

    @app.get("/raise/{name}")
    async def raise_(name: str) -> None:
        cls: type[AppError] = getattr(api_errors, name)
        raise cls()

    @app.get("/cursor")
    async def cursor(c: str) -> dict[str, str]:
        return {"id": str(decode_cursor(c))}

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_every_identity_code_has_its_status_and_shape(client: TestClient) -> None:
    """PAY-602: each §3.5 identity code renders as {error: {code, message, request_id}} with
    its status, and the module exports exactly these classes."""
    assert set(api_errors.__all__) == set(EXPECTED)
    for name, (code, status) in EXPECTED.items():
        r = client.get(f"/raise/{name}")
        assert r.status_code == status, name
        body = r.json()
        assert set(body) == {"error"}
        assert body["error"]["code"] == code
        assert body["error"]["message"]
        assert "request_id" in body["error"]


def _issues(exc: ValidationError) -> list[tuple[str, str]]:
    return [(".".join(str(p) for p in e["loc"]), e["type"]) for e in exc.errors()]


@pytest.mark.parametrize(
    ("body", "issue"),
    [
        ({"status": "active"}, ("status", "literal_error")),
        ({}, ("", "value_error")),
        ({"roles": None, "status": None}, ("", "value_error")),
        ({"roles": []}, ("roles", "too_short")),
        ({"roles": ["admin", "admin"]}, ("roles", "value_error")),
        ({"roles": ["owner"]}, ("roles.0", "enum")),
        ({"status": "inactive", "name": "x"}, ("name", "extra_forbidden")),
    ],
)
def test_patch_user_in_rejects(body: dict[str, Any], issue: tuple[str, str]) -> None:
    """PAY-108, R37, C25: status is "inactive" only (no reactivation); at least one field;
    roles non-empty, known and unique; no other field."""
    with pytest.raises(ValidationError) as exc:
        PatchUserIn.model_validate(body)
    assert issue in _issues(exc.value)


def test_patch_user_in_accepts() -> None:
    """PAY-108: roles, status, or both."""
    assert PatchUserIn.model_validate({"status": "inactive"}).roles is None
    both = PatchUserIn.model_validate({"roles": ["approver"], "status": "inactive"})
    assert (both.roles, both.status) == (["approver"], "inactive")


def test_reset_models() -> None:
    """PAY-105, C63: the reset request takes an email (NUL rejected); confirm a token and a
    1-256 character password (the 12-character policy is a domain check → 422
    password_too_weak)."""
    with pytest.raises(ValidationError) as exc:
        ResetRequestIn.model_validate({"email": "a\x00@b.example"})
    assert ("email", "invalid_character") in _issues(exc.value)
    assert ResetConfirmIn.model_validate({"token": "t", "password": "short"}).password == "short"
    with pytest.raises(ValidationError):
        ResetConfirmIn.model_validate({"token": "t" * 129, "password": "x" * 12})


def test_cursor_round_trip() -> None:
    """PAY-108, PAY-109, R4: a cursor is the 22-character base64url of the row's UUID; no
    cursor is the first page."""
    row = UUID("0193a000-0000-7000-8000-00000000abcd")
    c = encode_cursor(row)
    assert len(c) == 22
    assert "=" not in c
    assert decode_cursor(c) == row
    assert decode_cursor(None) is None


@pytest.mark.parametrize(
    "bad", ["", "abc", "A" * 21, "A" * 23, "A" * 21 + "!", "A" * 21 + "B", str(UUID(int=1))]
)
def test_bad_cursor_is_422_without_echo(client: TestClient, bad: str) -> None:
    """PAY-602, C14: an undecodable or non-canonical cursor → 422 validation_error with
    query.cursor / invalid_cursor; the value is never echoed."""
    with pytest.raises(ValidationFailed):
        decode_cursor(bad)
    r = client.get("/cursor", params={"c": bad})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error"
    assert err["details"] == [{"field": "query.cursor", "issue": "invalid_cursor"}]
    if bad:
        assert bad not in r.text.replace('"code"', "")
