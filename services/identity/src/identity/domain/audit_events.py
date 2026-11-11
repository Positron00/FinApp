"""Audit event names (identity LLD §2.6); the migration's ``ck_audit_log_event`` lists the same."""

from __future__ import annotations

from typing import Final

__all__ = [
    "ALL_EVENTS",
    "AUTH_LOCKOUT",
    "AUTH_PASSWORD_RESET_COMPLETED",
    "AUTH_PASSWORD_RESET_REQUESTED",
    "AUTH_SIGNIN_FAILED",
    "AUTH_SIGNIN_SUCCEEDED",
    "USER_BOOTSTRAPPED",
    "USER_DEACTIVATED",
    "USER_INVITED",
    "USER_INVITE_ACCEPTED",
    "USER_INVITE_RESENT",
    "USER_ROLES_CHANGED",
]

USER_BOOTSTRAPPED: Final = "user.bootstrapped"
USER_INVITED: Final = "user.invited"
USER_INVITE_RESENT: Final = "user.invite_resent"
USER_INVITE_ACCEPTED: Final = "user.invite_accepted"
AUTH_SIGNIN_SUCCEEDED: Final = "auth.signin.succeeded"
AUTH_SIGNIN_FAILED: Final = "auth.signin.failed"
AUTH_LOCKOUT: Final = "auth.lockout"
AUTH_PASSWORD_RESET_REQUESTED: Final = "auth.password_reset.requested"  # noqa: S105
AUTH_PASSWORD_RESET_COMPLETED: Final = "auth.password_reset.completed"  # noqa: S105
USER_ROLES_CHANGED: Final = "user.roles_changed"
USER_DEACTIVATED: Final = "user.deactivated"

ALL_EVENTS: Final[tuple[str, ...]] = (
    USER_BOOTSTRAPPED,
    USER_INVITED,
    USER_INVITE_RESENT,
    USER_INVITE_ACCEPTED,
    AUTH_SIGNIN_SUCCEEDED,
    AUTH_SIGNIN_FAILED,
    AUTH_LOCKOUT,
    AUTH_PASSWORD_RESET_REQUESTED,
    AUTH_PASSWORD_RESET_COMPLETED,
    USER_ROLES_CHANGED,
    USER_DEACTIVATED,
)
