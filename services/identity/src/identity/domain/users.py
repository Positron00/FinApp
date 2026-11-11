"""User-admin rules (identity LLD §6.5; PAY-107, PAY-108). Pure: no I/O.

``plan_user_update`` decides what a ``PATCH /v1/users/{id}`` does to a locked target row:
the self rule, the last-admin rule, role normalisation and the ``roles_version`` bump.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from identity.domain.roles import Role, normalise_roles

__all__ = [
    "PatchRequest",
    "TargetUser",
    "UpdateRefused",
    "UserChange",
    "plan_user_update",
    "roles_changed",
]

RefusalCode = Literal["cannot_modify_self", "last_admin"]


class UpdateRefused(Exception):  # noqa: N818  (a domain outcome, mapped to 409 by the use case)
    """The PATCH is refused with 409 ``code``."""

    def __init__(self, code: RefusalCode) -> None:
        self.code: RefusalCode = code
        super().__init__(code)


@dataclass(frozen=True)
class TargetUser:
    id: UUID
    roles: Sequence[str]
    status: str


@dataclass(frozen=True)
class PatchRequest:
    """``roles`` / ``deactivate`` as sent; ``None`` / ``False`` leave the field alone."""

    roles: Sequence[str] | None = None
    deactivate: bool = False


@dataclass(frozen=True)
class UserChange:
    """What to write. ``new_roles`` is ``None`` when the role set is unchanged."""

    old_roles: list[str]
    new_roles: list[str] | None
    deactivate: bool

    @property
    def bump(self) -> bool:
        """``roles_version`` += 1 (once per PATCH) when the roles or the status change (§6.5)."""
        return self.new_roles is not None or self.deactivate

    @property
    def noop(self) -> bool:
        return not self.bump


def roles_changed(old: Sequence[str], new: Sequence[str]) -> bool:
    """True when the role *sets* differ (order and duplicates don't count)."""
    return set(normalise_roles(old)) != set(normalise_roles(new))


def plan_user_update(
    actor_id: UUID, target: TargetUser, patch: PatchRequest, other_active_admin_count: int
) -> UserChange:
    """The change to apply, or ``UpdateRefused``.

    - ``cannot_modify_self`` when the actor is the target, whatever the patch.
    - ``last_admin`` when the target is an **active** admin, the change removes ``admin`` or
      deactivates it, and no *other* active admin remains. Pending admins don't count.
    - Deactivating an already inactive user, or sending the current role set, changes nothing.
    """
    if target.id == actor_id:
        raise UpdateRefused("cannot_modify_self")
    old = normalise_roles(target.roles)
    new: list[str] | None = None
    if patch.roles is not None:
        wanted = normalise_roles(patch.roles)
        if roles_changed(old, wanted):
            new = wanted
    deactivate = patch.deactivate and target.status != "inactive"
    change = UserChange(old_roles=old, new_roles=new, deactivate=deactivate)

    is_active_admin = target.status == "active" and Role.admin in old
    loses_admin = deactivate or (new is not None and Role.admin not in new)
    if is_active_admin and loses_admin and other_active_admin_count <= 0:
        raise UpdateRefused("last_admin")
    return change
