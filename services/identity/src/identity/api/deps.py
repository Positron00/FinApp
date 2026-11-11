"""FastAPI dependencies (identity LLD §3.2; PAY-600, PAY-601, C16, C30)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header
from pycommon.auth import Principal, require
from pycommon.errors import Forbidden, ValidationFailed

from identity.db.repositories import UserRepo
from identity.domain.ip import normalise_ip
from identity.runtime import IdentityRuntime, get_runtime

__all__ = [
    "ActorUser",
    "AdminActor",
    "ClientIp",
    "PublicCaller",
    "Runtime",
    "UserCaller",
    "admin_actor",
    "client_ip",
    "public_caller",
    "user_caller",
]

# Only service:web on the four public routes; only user tokens elsewhere (403 otherwise; C30).
# The keyset holds web keys only, and require() rejects any other issuer with 401.
public_caller = require(issuers=("web",), subjects=("service:web",))
user_caller = require(issuers=("web",), subjects=("user",))

PublicCaller = Annotated[Principal, Depends(public_caller)]
UserCaller = Annotated[Principal, Depends(user_caller)]
Runtime = Annotated[IdentityRuntime, Depends(get_runtime)]


@dataclass(frozen=True)
class ActorUser:
    user_id: UUID
    company_id: UUID


async def admin_actor(principal: UserCaller, rt: Runtime) -> ActorUser:
    """The actor row must be active and hold ``admin`` **in the DB** (JWT roles may be stale)."""
    uid = principal.user_id
    if uid is None:  # pragma: no cover  (user_caller guarantees a UUID subject)
        raise Forbidden()
    async with rt.sessions() as session:
        user = await UserRepo(session).get(principal.cid, uid)
    if user is None or user.status != "active" or "admin" not in user.roles:
        raise Forbidden()
    return ActorUser(user_id=uid, company_id=principal.cid)


AdminActor = Annotated[ActorUser, Depends(admin_actor)]


async def client_ip(
    x_client_ip: Annotated[str | None, Header(alias="X-Client-IP", max_length=64)] = None,
) -> str | None:
    """The browser IP as the BFF derived it (R18); ``None`` when absent (C16)."""
    if x_client_ip is None:
        return None
    try:
        return normalise_ip(x_client_ip)
    except ValueError:
        raise ValidationFailed(
            details=[{"field": "header.X-Client-IP", "issue": "ip_address"}]
        ) from None


ClientIp = Annotated[str | None, Depends(client_ip)]
