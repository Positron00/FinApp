"""Error format and handler mapping (platform LLD §3.4; PAY-602, C14, C56)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any

import asyncpg  # type: ignore[import-untyped]
import pytest
import redis.exceptions
import sqlalchemy.exc
from fastapi import APIRouter, FastAPI, Header, Query
from fastapi.testclient import TestClient
from pycommon.app import create_app
from pycommon.db import map_db_errors
from pycommon.errors import (
    AppError,
    BadRequest,
    Conflict,
    DbTimeout,
    ErrorBody,
    NotFound,
    RateLimited,
    ServiceUnavailable,
    ValidationFailed,
    db_error_kind,
    install_handlers,
    is_invalid_text,
)
from pycommon.testing import JsonLogCapture
from pydantic import BaseModel
from starlette.exceptions import HTTPException

from tests.helpers import make_settings

SECRET_DETAIL = "internal-detail-do-not-leak"

# Functions whose frames belong to a module named "asyncpg.*", as if the driver raised the error.
_DRIVER_SHIM: dict[str, Any] = {"__name__": "asyncpg.test_shim"}
exec(  # noqa: S102  (fixed test code: fakes a traceback frame inside the driver)
    "def refuse():\n    raise ConnectionRefusedError(61, 'refused')\n"
    "def time_out():\n    raise TimeoutError\n",
    _DRIVER_SHIM,
)


class Entry(BaseModel):
    employee_id: str
    hours_hundredths: int


class Body(BaseModel):
    email: str
    entries: list[Entry]


def _router() -> APIRouter:
    r = APIRouter()

    @r.get("/app-error/{kind}")
    async def app_error(kind: str) -> None:
        errors: dict[str, AppError] = {
            "bad": BadRequest(),
            "notfound": NotFound("Pay run not found.", code="pay_run_not_found"),
            "conflict": Conflict(details={"employee_ids": ["e1"]}),
            "limited": RateLimited(retry_after_s=7),
            "unavailable": ServiceUnavailable(),
            "validation": ValidationFailed(details=[{"field": "body.x", "issue": "bad"}]),
        }
        raise errors[kind]

    @r.post("/validate")
    async def validate(
        body: Body,
        limit: Annotated[int, Query()] = 10,
        key: Annotated[str | None, Header(alias="Idempotency-Key", pattern="^[a-z]+$")] = None,
    ) -> dict[str, int]:
        return {"limit": limit}

    @r.get("/driver/{kind}")
    async def driver(kind: str) -> None:  # noqa: PLR0912  (one branch per error kind)
        if kind == "query_canceled":
            raise asyncpg.QueryCanceledError("canceling statement due to statement timeout")
        if kind == "pool_timeout":
            raise sqlalchemy.exc.TimeoutError("QueuePool limit reached")
        if kind == "connection":
            raise sqlalchemy.exc.OperationalError(
                "SELECT 1", {}, asyncpg.ConnectionDoesNotExistError("closed")
            )
        if kind == "oserror":
            raise ConnectionRefusedError(61, "refused")
        if kind == "driver_oserror":
            _DRIVER_SHIM["refuse"]()
        if kind == "driver_timeout":
            _DRIVER_SHIM["time_out"]()
        if kind == "bare_timeout":
            raise TimeoutError
        if kind == "nul":
            raise asyncpg.CharacterNotInRepertoireError("invalid byte sequence 0x00")
        if kind == "nul_wrapped":
            raise sqlalchemy.exc.DataError(
                "INSERT", {}, asyncpg.CharacterNotInRepertoireError("0x00")
            )
        if kind == "mapped_timeout":
            with map_db_errors("load"):
                raise TimeoutError
        if kind == "redis":
            raise redis.exceptions.ConnectionError("redis down")
        if kind == "redis_timeout":
            raise redis.exceptions.TimeoutError("slow")
        if kind == "programming":
            raise sqlalchemy.exc.ProgrammingError("SELECT x", {}, Exception(SECRET_DETAIL))
        raise RuntimeError(SECRET_DETAIL)

    return r


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(
        make_settings(),
        public_routers=[_router()],
        use_db=False,
        use_redis=False,
        use_jwt=False,
        instrument=False,
    )
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _error(resp: Any) -> dict[str, Any]:
    body = resp.json()
    ErrorBody.model_validate(body)
    assert set(body) == {"error"}
    assert body["error"]["request_id"] == resp.headers["x-request-id"]
    err: dict[str, Any] = body["error"]
    return err


@pytest.mark.parametrize(
    ("kind", "status", "code"),
    [
        ("bad", 400, "bad_request"),
        ("notfound", 404, "pay_run_not_found"),
        ("conflict", 409, "conflict"),
        ("limited", 429, "rate_limited"),
        ("unavailable", 503, "service_unavailable"),
        ("validation", 422, "validation_error"),
    ],
)
def test_app_errors_use_their_status_and_code(
    client: TestClient, caplog_json: JsonLogCapture, kind: str, status: int, code: str
) -> None:
    """PAY-602: an AppError gives its status, code and the {error: {…}} body with request_id."""
    resp = client.get(f"/app-error/{kind}")
    assert resp.status_code == status
    err = _error(resp)
    assert err["code"] == code
    assert err["message"]
    (event,) = caplog_json.events("http.error")
    assert event["level"] == ("warning" if status >= 500 else "info")
    assert (event["code"], event["status"]) == (code, status)


def test_details_and_retry_after(client: TestClient) -> None:
    """PAY-602: details pass through; 429 sets Retry-After; details is absent when unset."""
    assert _error(client.get("/app-error/conflict"))["details"] == {"employee_ids": ["e1"]}
    limited = client.get("/app-error/limited")
    assert limited.headers["retry-after"] == "7"
    assert "details" not in _error(client.get("/app-error/bad"))
    assert NotFound("x").message == "x"
    assert RateLimited(retry_after_s=0).retry_after_s == 1


def test_validation_error_fields_are_dotted_and_never_echo_input(client: TestClient) -> None:
    """PAY-602 / C14 / C56: 422 validation_error, details [{field, issue}], no submitted values."""
    secret_value = "not-an-int-secret-123"
    resp = client.post(
        "/validate?limit=abc",
        json={"email": 12345, "entries": [{"employee_id": "e", "hours_hundredths": secret_value}]},
        headers={"Idempotency-Key": "UPPER-1"},
    )
    assert resp.status_code == 422
    err = _error(resp)
    assert err["code"] == "validation_error"
    fields = {d["field"]: d["issue"] for d in err["details"]}
    assert fields == {
        "body.email": "string_type",
        "body.entries.0.hours_hundredths": "int_parsing",
        "query.limit": "int_parsing",
        "header.Idempotency-Key": "string_pattern_mismatch",
    }
    assert all(set(d) == {"field", "issue"} for d in err["details"])
    assert secret_value not in resp.text
    assert "12345" not in resp.text
    assert "UPPER-1" not in resp.text


def test_starlette_404_and_405(client: TestClient) -> None:
    """PAY-602: unknown route → 404 not_found; wrong method → 405 method_not_allowed."""
    missing = client.get("/nope")
    assert (missing.status_code, _error(missing)["code"]) == (404, "not_found")
    wrong = client.put("/validate")
    assert (wrong.status_code, _error(wrong)["code"]) == (405, "method_not_allowed")
    assert "allow" in wrong.headers


@pytest.mark.parametrize(
    ("kind", "code", "event"),
    [
        ("query_canceled", "db_timeout", "db.timeout"),
        ("pool_timeout", "db_timeout", "db.timeout"),
        ("connection", "db_unavailable", "db.unavailable"),
        ("driver_oserror", "db_unavailable", "db.unavailable"),
        ("driver_timeout", "db_timeout", "db.timeout"),
        ("mapped_timeout", "db_timeout", "db.timeout"),
        ("redis", "service_unavailable", "redis.unavailable"),
        ("redis_timeout", "service_unavailable", "redis.unavailable"),
    ],
)
def test_driver_errors_map_to_503(
    client: TestClient, caplog_json: JsonLogCapture, kind: str, code: str, event: str
) -> None:
    """PAY-602 / PAY-605: DB timeout → db_timeout, connection → db_unavailable, Redis → 503."""
    resp = client.get(f"/driver/{kind}")
    assert resp.status_code == 503
    assert _error(resp)["code"] == code
    (logged,) = caplog_json.events(event)
    assert logged["level"] == "warning"
    expected = "load" if kind == "mapped_timeout" else "GET /driver/{kind}"
    assert logged["operation"] == expected


@pytest.mark.parametrize("kind", ["boom", "programming", "oserror", "bare_timeout"])
def test_unhandled_errors_are_500_without_internals(
    client: TestClient, caplog_json: JsonLogCapture, kind: str
) -> None:
    """PAY-602, C63: any other exception → 500 internal_error; traceback in logs, never the body.

    A bare OSError or TimeoutError raised outside the DB driver is not a DB error (C63).
    """
    resp = client.get(f"/driver/{kind}")
    assert resp.status_code == 500
    err = _error(resp)
    assert err == {
        "code": "internal_error",
        "message": "Something went wrong.",
        "request_id": resp.headers["x-request-id"],
    }
    assert SECRET_DETAIL not in resp.text
    assert "Traceback" not in resp.text
    (event,) = caplog_json.events("http.unhandled")
    assert event["level"] == "error"
    assert "Traceback" in event["exc"]


def test_install_handlers_alone_handles_unhandled_errors(caplog_json: JsonLogCapture) -> None:
    """PAY-602: without create_app's middleware, the Exception handler still gives the 500 body."""
    app = FastAPI()
    install_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError(SECRET_DETAIL)

    @app.get("/db")
    async def db() -> None:
        raise ValueError("x") from asyncpg.QueryCanceledError("c")

    @app.get("/teapot")
    async def teapot() -> None:
        raise HTTPException(status_code=418)

    @app.get("/gateway")
    async def gateway() -> None:
        raise HTTPException(status_code=502)

    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/boom")
        assert resp.status_code == 500
        assert resp.json()["error"]["code"] == "internal_error"
        assert resp.json()["error"]["request_id"] is None
        assert SECRET_DETAIL not in resp.text
        assert c.get("/db").json()["error"]["code"] == "db_timeout"
        assert c.get("/teapot").json()["error"]["code"] == "http_error"
        assert c.get("/gateway").json()["error"]["code"] == "internal_error"
    assert caplog_json.events("http.unhandled")


def test_db_error_kind_classification() -> None:
    """PAY-605: the classifier walks causes and SQLSTATEs."""

    class PgError(Exception):
        def __init__(self, sqlstate: str) -> None:
            super().__init__("x")
            self.sqlstate = sqlstate

    assert db_error_kind(TimeoutError()) is None  # C63: not raised inside the driver
    assert db_error_kind(ConnectionRefusedError()) is None
    assert db_error_kind(TimeoutError(), in_db_call=True) == "timeout"
    assert db_error_kind(ConnectionRefusedError(), in_db_call=True) == "unavailable"
    httpish = RuntimeError("connect failed")
    httpish.__context__ = OSError("refused")
    assert db_error_kind(httpish) is None
    assert db_error_kind(PgError("57014")) == "timeout"
    assert db_error_kind(PgError("55P03")) == "timeout"  # lock_timeout (S5 finding)
    assert db_error_kind(PgError("08006")) == "unavailable"
    assert db_error_kind(PgError("23505")) is None
    wrapped = sqlalchemy.exc.DBAPIError(
        "SELECT 1", {}, Exception("gone"), connection_invalidated=True
    )
    assert db_error_kind(wrapped) == "unavailable"
    outer = sqlalchemy.exc.OperationalError("SELECT", {}, Exception("x"))
    outer.__cause__ = asyncpg.QueryCanceledError("c")
    assert db_error_kind(outer) == "timeout"
    assert db_error_kind(ValueError("x")) is None
    with pytest.raises(DbTimeout), map_db_errors():
        raise DbTimeout()
    with pytest.raises(KeyError), map_db_errors():
        raise KeyError("not a db error")


@pytest.mark.parametrize("kind", ["nul", "nul_wrapped"])
def test_invalid_text_is_422_validation_error(client: TestClient, kind: str) -> None:
    """C63: SQLSTATE 22021 (a NUL byte in text) → 422 validation_error, never 500 or 503."""
    resp = client.get(f"/driver/{kind}")
    assert resp.status_code == 422
    err = _error(resp)
    assert err["code"] == "validation_error"
    assert err["details"] == [{"field": "body", "issue": "invalid_character"}]
    assert "0x00" not in resp.text


def test_map_db_errors_maps_invalid_text_to_validation_failed() -> None:
    """C63: inside a DB call, 22021 raises ValidationFailed (422)."""
    with pytest.raises(ValidationFailed) as info, map_db_errors("save"):
        raise asyncpg.CharacterNotInRepertoireError("0x00")
    assert info.value.status == 422
    assert is_invalid_text(info.value.__cause__ or ValueError())
    assert not is_invalid_text(ValueError("x"))
    with pytest.raises(ValidationFailed), map_db_errors():
        raise ValidationFailed()
