"""Identity error codes (identity LLD §3.5), rendered by pycommon's handler (PAY-602)."""

from __future__ import annotations

from pycommon.errors import AppError, BadRequest, Conflict, NotFound, Unauthorized, ValidationFailed

__all__ = [
    "CannotModifySelf",
    "IdentityError",
    "InvalidCredentials",
    "InviteInvalid",
    "InviteNotFound",
    "InviteNotPending",
    "LastAdmin",
    "PasswordTooWeak",
    "ResetInvalid",
    "UserEmailTaken",
    "UserNotFound",
]

IdentityError = AppError


class InvalidCredentials(Unauthorized):
    code = "invalid_credentials"
    default_message = "The email or password is incorrect."


class InviteInvalid(BadRequest):
    code = "invite_invalid"
    default_message = "This invite link is invalid or has expired."


class ResetInvalid(BadRequest):
    code = "reset_invalid"
    default_message = "This reset link is invalid or has expired."


class PasswordTooWeak(ValidationFailed):
    code = "password_too_weak"
    default_message = "The password must be at least 12 characters long."


class UserEmailTaken(Conflict):
    code = "user_email_taken"
    default_message = "A user with this email already exists."


class InviteNotPending(Conflict):
    code = "invite_not_pending"
    default_message = "This user has already accepted an invite."


class CannotModifySelf(Conflict):
    code = "cannot_modify_self"
    default_message = "You can't change your own roles or status."


class LastAdmin(Conflict):
    code = "last_admin"
    default_message = "The company must keep at least one active admin."


class InviteNotFound(NotFound):
    code = "invite_not_found"
    default_message = "Invite not found."


class UserNotFound(NotFound):
    code = "user_not_found"
    default_message = "User not found."
