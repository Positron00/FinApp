"""Mapping helpers shared by the use cases."""

from __future__ import annotations

from uuid import UUID

from identity.api.schemas import MeOut, SessionUser, UserOut
from identity.db.models import User
from identity.domain.roles import Role

__all__ = ["me_out", "session_user", "user_out"]


def session_user(u: User) -> SessionUser:
    return SessionUser(
        user_id=u.id,
        company_id=u.company_id,
        email=u.email,
        name=u.name,
        roles=[Role(r) for r in u.roles],
        roles_version=u.roles_version,
        status="active",
    )


def _fields(u: User, pending_invite_id: UUID | None) -> dict[str, object]:
    return {
        "id": u.id,
        "email": u.email,
        "name": u.name,
        "roles": [Role(r) for r in u.roles],
        "roles_version": u.roles_version,
        "status": u.status,
        "created_at": u.created_at,
        "updated_at": u.updated_at,
        "activated_at": u.activated_at,
        "deactivated_at": u.deactivated_at,
        "pending_invite_id": pending_invite_id if u.status == "pending" else None,
    }


def user_out(u: User, pending_invite_id: UUID | None) -> UserOut:
    return UserOut.model_validate(_fields(u, pending_invite_id))


def me_out(u: User, pending_invite_id: UUID | None) -> MeOut:
    return MeOut.model_validate({**_fields(u, pending_invite_id), "company_id": u.company_id})
