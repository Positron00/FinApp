"""No secret reaches logs or spans during a full request (platform LLD §6.1; PAY-NFR-07)."""

import asyncio
import json
import logging
import secrets
from collections.abc import Iterator
from typing import Annotated, Any

import asyncpg  # type: ignore[import-untyped]
import pytest
from fastapi import APIRouter, Depends, Request
from fastapi.testclient import TestClient
from pycommon.app import AppContext, create_app, get_ctx
from pycommon.logging import get_logger, log_event
from pycommon.testing import OtelMemory, TokenFactory
from pydantic import SecretStr
from sqlalchemy import text

from tests.helpers import COMPANY_ID, SvcSettings
from tests.integration.conftest import Pg, RedisServer

pytestmark = pytest.mark.integration

USER = "0193a000-0000-7000-8000-0000000000aa"


class SecretSettings(SvcSettings):
    testsvc_api_key: SecretStr
    testsvc_field_keys: SecretStr


def _marker(name: str) -> str:
    return f"SECRET-{name}-{secrets.token_hex(8)}"


@pytest.fixture
def secret_values(pg: Pg, redis_server: RedisServer) -> Iterator[dict[str, str]]:
    values = {k: _marker(k) for k in ("db", "redis", "api", "field", "query", "body")}

    async def role() -> None:
        conn = await asyncpg.connect(**pg.dsn())
        try:
            await conn.execute(f"CREATE ROLE pyc_secret LOGIN PASSWORD '{values['db']}'")
            await conn.execute("GRANT pyc_app TO pyc_secret")
        finally:
            await conn.close()

    asyncio.run(role())
    with redis_server.client() as admin:
        admin.execute_command(
            "ACL", "SETUSER", "secretuser", "reset", "on", f">{values['redis']}",
            "~testsvc:*", "+@all",
        )  # fmt: skip
    yield values

    async def drop() -> None:
        conn = await asyncpg.connect(**pg.dsn())
        try:
            await conn.execute("DROP ROLE pyc_secret")
        finally:
            await conn.close()

    asyncio.run(drop())
    with redis_server.client() as admin:
        admin.execute_command("ACL", "DELUSER", "secretuser")


def _router() -> APIRouter:
    r = APIRouter()

    @r.post("/v1/work")
    async def work(
        request: Request, ctx: Annotated[AppContext, Depends(get_ctx)]
    ) -> dict[str, Any]:
        assert ctx.sessions is not None
        assert ctx.redis is not None
        async with ctx.sessions() as s:
            value = (await s.execute(text("SELECT 1"))).scalar_one()
        await ctx.redis.set("testsvc:k", "v", ex=10)
        body = await request.json()
        log_event(
            get_logger("testsvc"),
            logging.INFO,
            "work.done",
            settings=ctx.settings.model_dump(),
            payload=body,
            authorization=request.headers.get("authorization"),
        )
        return {"value": value}

    @r.get("/v1/explode")
    async def explode(ctx: Annotated[AppContext, Depends(get_ctx)]) -> None:
        raise RuntimeError(str(ctx.settings.secret_fields()))

    return r


def test_secrets_never_reach_stdout_or_spans(
    pg: Pg,
    redis_server: RedisServer,
    secret_values: dict[str, str],
    otel_memory: OtelMemory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """PAY-NFR-07: with every secret set to a recognisable value, none is logged or traced."""
    tf = TokenFactory.create()
    settings = SecretSettings.model_validate(
        {
            **pg.settings(redis_host=redis_server.host, redis_port=redis_server.port).model_dump(),
            "db_user": "pyc_secret",
            "db_password": SecretStr(secret_values["db"]),
            "redis_username": "secretuser",
            "redis_password": SecretStr(secret_values["redis"]),
            "internal_jwt_public_keys": tf.keyset,
            "testsvc_api_key": SecretStr(secret_values["api"]),
            "testsvc_field_keys": SecretStr(secret_values["field"]),
            "company_id": COMPANY_ID,
        }
    )
    token = tf.mint(aud="testsvc", sub=USER, cid=COMPANY_ID, name="N", sid="sid-x")
    app = create_app(settings, routers=[_router()], instrument=True)
    with TestClient(app, raise_server_exceptions=False) as c:
        headers = {"Authorization": f"Bearer {token}"}
        resp = c.post(
            f"/v1/work?token={secret_values['query']}",
            json={"password": secret_values["body"], "note": "hello"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert c.get("/v1/explode", headers=headers).status_code == 500
        assert c.get("/readyz").json()["checks"] == {"db": "ok", "redis": "ok"}
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert any(line.get("event") == "work.done" for line in lines)
    assert any(line.get("event") == "http.unhandled" for line in lines)
    otel_memory.logger_provider.force_flush()
    exported = json.dumps(
        [
            {
                "name": s.name,
                "attrs": dict(s.attributes or {}),
                "events": [str(e.attributes) for e in s.events],
            }
            for s in otel_memory.spans()
        ]
        + [
            str(r.log_record.attributes) + str(r.log_record.body) for r in otel_memory.log_records()
        ],
        default=str,
    )
    assert otel_memory.spans()
    for name, value in secret_values.items():
        assert value not in out, name
        assert value not in exported, name
    assert token not in out
    assert token not in exported
    assert "sid-x" not in out
