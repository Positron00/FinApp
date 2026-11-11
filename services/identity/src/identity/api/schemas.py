"""Request and response models (identity LLD §3.3).

The F1 models (reset, resend, patch, lists, audit) are added with their endpoints.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, field_validator

from identity.domain.roles import Role

__all__ = [
    "Email",
    "InviteAcceptIn",
    "InviteUserIn",
    "LoginIn",
    "MeOut",
    "Name",
    "Password",
    "RoleList",
    "SessionUser",
    "Token",
    "UserOut",
]

# Dev and seed accounts use the RFC 2606 domain payrun.example.com (C59), which email-validator
# accepts, so no special-use override is needed.

Email = Annotated[EmailStr, StringConstraints(max_length=254)]
# The policy (>= 12) is checked in the domain, so it maps to 422 password_too_weak.
Password = Annotated[str, StringConstraints(min_length=1, max_length=256)]
# The format is checked in the domain, so it maps to 400 invite_invalid / reset_invalid.
Token = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
RoleList = Annotated[list[Role], Field(min_length=1, max_length=3)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class LoginIn(_In):
    email: Email
    password: Password


class InviteAcceptIn(_In):
    token: Token
    password: Password


class InviteUserIn(_In):
    email: Email
    name: Name
    roles: RoleList

    @field_validator("roles")
    @classmethod
    def _no_duplicates(cls, v: list[Role]) -> list[Role]:
        if len(set(v)) != len(v):
            raise ValueError("duplicate roles")
        return v


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
