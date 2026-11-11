"""The ``identity`` Redis ACL user (identity LLD §3.7; PAY-607, C3)."""

from __future__ import annotations

import pytest
import redis.asyncio as aioredis
from pycommon.testing import RedisAcl
from redis.exceptions import NoPermissionError


async def test_identity_user_scope(redis_acl: RedisAcl) -> None:
    """PAY-607, C3: identity:* works with MULTI/INCR/EXPIRE NX, SET NX EX and PTTL; web:*,
    the payout stream and EVAL are NOPERM."""
    e = redis_acl.env_for("identity")
    r = aioredis.Redis(
        host=e["REDIS_HOST"],
        port=int(e["REDIS_PORT"]),
        username="identity",
        password=e["REDIS_PASSWORD"],
    )
    try:
        async with r.pipeline(transaction=True) as pipe:
            pipe.incr("identity:rl:t:1")
            pipe.expire("identity:rl:t:1", 10, nx=True)
            count, _ = await pipe.execute()
        assert count == 1
        assert await r.set("identity:rl:login:lock:x", "1", nx=True, ex=60)
        assert await r.pttl("identity:rl:login:lock:x") > 0
        with pytest.raises(NoPermissionError):
            await r.get("web:sess:x")
        with pytest.raises(NoPermissionError):
            await r.xadd("stream:payout.requested", {"a": "b"})
        with pytest.raises(NoPermissionError):
            await r.eval("return 1", 0)
    finally:
        await r.aclose()
