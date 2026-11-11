"""Per-app identity state built in ``on_startup`` (hasher, limits, metrics, relay)."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request
from pycommon import Clock
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity.domain.ip import email_key, ip_hash
from identity.domain.passwords import PasswordHasher
from identity.email.relay import EmailRelay
from identity.limits import AuthLimits
from identity.metrics import IdentityMetrics
from identity.settings import IdentitySettings

__all__ = ["IdentityRuntime", "get_runtime"]


@dataclass
class IdentityRuntime:
    settings: IdentitySettings
    sessions: async_sessionmaker[AsyncSession]
    redis: Redis
    hasher: PasswordHasher
    limits: AuthLimits
    metrics: IdentityMetrics
    relay: EmailRelay
    clock: Clock

    @property
    def _key(self) -> bytes:
        return self.settings.audit_ip_hash_key.get_secret_value().encode()

    def ip_hash(self, ip: str | None) -> str | None:
        return None if ip is None else ip_hash(ip, self._key)

    def email_key(self, email: str) -> str:
        return email_key(email, self._key)


def get_runtime(request: Request) -> IdentityRuntime:
    rt = getattr(request.app.state, "identity", None)
    if not isinstance(rt, IdentityRuntime):
        raise RuntimeError("the identity app has not started")
    return rt
