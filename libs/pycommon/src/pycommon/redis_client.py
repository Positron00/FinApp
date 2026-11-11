"""Redis client factory, one per ACL user (platform LLD §3.8; PAY-607).

Blocking reads need a second client: ``XREADGROUP … BLOCK 5000`` would always hit the 500 ms socket
timeout, so the worker builds its blocking client with
``create_redis(settings, socket_timeout_ms=PAYOUT_XREAD_BLOCK_MS + REDIS_SOCKET_TIMEOUT_MS)``.
"""

from __future__ import annotations

from typing import Final

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from pycommon.settings import BaseServiceSettings

__all__ = ["create_redis"]

HEALTH_CHECK_INTERVAL_S: Final = 15


def create_redis(
    settings: BaseServiceSettings,
    *,
    socket_timeout_ms: int | None = None,
    client_name: str | None = None,
) -> Redis:
    """A ``redis.asyncio`` client as ``REDIS_USERNAME``; no automatic retries (callers decide)."""
    if not settings.redis_host:
        raise ValueError("REDIS_HOST is not set: this service has no Redis")
    timeout_s = (socket_timeout_ms or settings.redis_socket_timeout_ms) / 1000
    password = settings.redis_password.get_secret_value() if settings.redis_password else None
    return Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        username=settings.redis_username,
        password=password,
        socket_connect_timeout=timeout_s,
        socket_timeout=timeout_s,
        health_check_interval=HEALTH_CHECK_INTERVAL_S,
        # redis-py >= 6 retries timeouts by default; "retry_on_timeout=False" means zero retries.
        retry=Retry(NoBackoff(), retries=0),
        decode_responses=False,
        client_name=client_name or settings.service_name,
    )
