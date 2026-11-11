"""User reads (identity LLD §3.4; PAY-107). List and PATCH arrive with F1."""

from __future__ import annotations

from uuid import UUID

from identity.api.errors import UserNotFound
from identity.api.schemas import MeOut
from identity.db.repositories import InviteRepo, UserRepo
from identity.runtime import IdentityRuntime
from identity.usecases.common import me_out

__all__ = ["get_me"]


async def get_me(rt: IdentityRuntime, cid: UUID, user_id: UUID) -> MeOut:
    """The caller's row, whatever its status (the BFF ends sessions that aren't active)."""
    async with rt.sessions() as session:
        user = await UserRepo(session).get(cid, user_id)
        if user is None:
            raise UserNotFound()
        pending = await InviteRepo(session).pending_id_for(cid, user_id)
    return me_out(user, pending)
