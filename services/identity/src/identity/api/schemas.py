"""Request and response models (identity LLD §3.3)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from identity.domain.roles import Role

__all__ = [
    "AuditEntryOut",
    "AuditListOut",
    "Email",
    "InviteAcceptIn",
    "InviteUserIn",
    "LoginIn",
    "MeOut",
    "Name",
    "Password",
    "PatchUserIn",
    "PendingInviteOut",
    "ResetConfirmIn",
    "ResetConfirmOut",
    "ResetRequestIn",
    "RoleList",
    "SessionUser",
    "Token",
    "UserListOut",
    "UserOut",
    "reject_nul",
]

# Dev and seed accounts use the RFC 2606 domain payrun.example.com (C59), which email-validator
# accepts, so no special-use override is needed.


def reject_nul(v: object) -> object:
    """Free text never holds NUL (Postgres can't store it; C63): 422 ``invalid_character``."""
    if isinstance(v, str) and "\x00" in v:
        raise PydanticCustomError("invalid_character", "must not contain NUL characters")
    return v


NoNul = BeforeValidator(reject_nul)
Email = Annotated[EmailStr, StringConstraints(max_length=254), NoNul]
# The policy (>= 12) is checked in the domain, so it maps to 422 password_too_weak.
Password = Annotated[str, StringConstraints(min_length=1, max_length=256)]
# The format is checked in the domain, so it maps to 400 invite_invalid / reset_invalid.
Token = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200), NoNul]
RoleList = Annotated[list[Role], Field(min_length=1, max_length=3)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class LoginIn(_In):
    email: Email
    password: Password


class InviteAcceptIn(_In):
    token: Token
    password: Password


def _no_duplicate_roles(v: list[Role] | None) -> list[Role] | None:
    if v is not None and len(set(v)) != len(v):
        raise ValueError("duplicate roles")
    return v


class InviteUserIn(_In):
    email: Email
    name: Name
    roles: RoleList

    _no_duplicates = field_validator("roles")(_no_duplicate_roles)


class ResetRequestIn(_In):
    email: Email


class ResetConfirmIn(_In):
    token: Token
    password: Password


class PatchUserIn(_In):
    """``roles`` and/or ``status``; only ``"inactive"`` (no reactivation in 0.1.0: R37, C25)."""

    roles: RoleList | None = None
    status: Literal["inactive"] | None = None

    _no_duplicates = field_validator("roles")(_no_duplicate_roles)

    @model_validator(mode="after")
    def _at_least_one(self) -> Self:
        if self.roles is None and self.status is None:
            raise ValueError("set roles or status")
        return self


class SessionUser(BaseModel):
    """Returned by login and accept (C6); the BFF maps it to ``Me`` (``id = user_id``)."""

    user_id: UUID
    company_id: UUID
    email: str
    name: str
    roles: list[Role]
    roles_version: int
    status: Literal["active"]


class UserOut(BaseModel):
    id: UUID
    email: str
    name: str
    roles: list[Role]
    roles_version: int
    status: Literal["pending", "active", "inactive"]
    created_at: datetime
    updated_at: datetime
    activated_at: datetime | None
    deactivated_at: datetime | None
    # Flat (C11, R21): the user's pending invite; None unless status == "pending".
    pending_invite_id: UUID | None


class MeOut(UserOut):
    """``GET /v1/users/me`` (R19): adds ``company_id`` for the BFF's re-check."""

    company_id: UUID


class UserListOut(BaseModel):
    items: list[UserOut]
    next_cursor: str | None


class PendingInviteOut(BaseModel):
    """Returned by resend: the new invite."""

    id: UUID
    expires_at: datetime | None  # None until the relay has sent it (C31)
    # The invite's latest outbox row; None once purged.
    email_status: Literal["pending", "sent", "failed", "cancelled"] | None


class ResetConfirmOut(BaseModel):
    """The BFF deletes every session in ``web:user-sess:<user_id>`` (R19)."""

    user_id: UUID
    roles_version: int


class AuditEntryOut(BaseModel):
    id: UUID
    at: datetime
    actor_user_id: UUID | None
    event: str
    target_user_id: UUID | None
    ip_hash: str | None
    details: dict[str, Any]


class AuditListOut(BaseModel):
    items: list[AuditEntryOut]
    next_cursor: str | None
