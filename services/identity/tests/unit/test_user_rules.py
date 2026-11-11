"""User-admin rules (identity LLD §6.5; PAY-107, PAY-108)."""

from __future__ import annotations

from uuid import UUID

import pytest
from identity.domain.users import (
    PatchRequest,
    TargetUser,
    UpdateRefused,
    plan_user_update,
    roles_changed,
)

ACTOR = UUID("0193a000-0000-7000-8000-0000000000a1")
TARGET = UUID("0193a000-0000-7000-8000-0000000000b2")


def target(roles: tuple[str, ...] = ("admin",), status: str = "active") -> TargetUser:
    return TargetUser(id=TARGET, roles=roles, status=status)


def test_cannot_modify_self_whatever_the_patch() -> None:
    """PAY-108: an admin can't change their own roles or status (cannot_modify_self), even a
    no-op patch, and even with other admins present."""
    me = TargetUser(id=ACTOR, roles=("admin",), status="active")
    for patch in (PatchRequest(roles=["approver"]), PatchRequest(deactivate=True), PatchRequest()):
        with pytest.raises(UpdateRefused) as exc:
            plan_user_update(ACTOR, me, patch, other_active_admin_count=5)
        assert exc.value.code == "cannot_modify_self"


@pytest.mark.parametrize(
    "patch",
    [PatchRequest(roles=["approver"]), PatchRequest(deactivate=True)],
    ids=["demote", "deactivate"],
)
def test_last_admin_for_demote_and_deactivate(patch: PatchRequest) -> None:
    """PAY-108: removing admin from, or deactivating, the last active admin → last_admin; with
    another active admin it is allowed."""
    with pytest.raises(UpdateRefused) as exc:
        plan_user_update(ACTOR, target(), patch, other_active_admin_count=0)
    assert exc.value.code == "last_admin"
    change = plan_user_update(ACTOR, target(), patch, other_active_admin_count=1)
    assert change.bump


@pytest.mark.parametrize("status", ["pending", "inactive"])
def test_pending_and_inactive_admins_do_not_count(status: str) -> None:
    """PAY-108: only an active admin is protected; demoting a pending (or inactive) admin is
    fine with no other active admin."""
    change = plan_user_update(
        ACTOR, target(status=status), PatchRequest(roles=["approver"]), other_active_admin_count=0
    )
    assert change.new_roles == ["approver"]


def test_keeping_admin_is_not_a_last_admin_case() -> None:
    """PAY-108: adding roles to the last admin keeps an admin, so it is allowed."""
    change = plan_user_update(
        ACTOR, target(), PatchRequest(roles=["approver", "admin"]), other_active_admin_count=0
    )
    assert change.new_roles == ["admin", "approver"]


def test_roles_version_bumps_only_on_a_real_change() -> None:
    """PAY-107, PAY-108: the same role set (any order) or deactivating an inactive user is a
    no-op; a new role set or a deactivation bumps once, also when both are in one patch."""
    t = target(roles=("approver", "admin"))
    same = plan_user_update(ACTOR, t, PatchRequest(roles=["admin", "approver"]), 1)
    assert (same.noop, same.new_roles) == (True, None)
    gone = plan_user_update(ACTOR, target(status="inactive"), PatchRequest(deactivate=True), 0)
    assert gone.noop
    both = plan_user_update(ACTOR, t, PatchRequest(roles=["approver"], deactivate=True), 1)
    assert (both.bump, both.deactivate, both.new_roles) == (True, True, ["approver"])


def test_roles_are_sorted_and_deduplicated() -> None:
    """PAY-108: stored roles are sorted and de-duplicated (§2.2); old_roles is normalised too."""
    change = plan_user_update(
        ACTOR,
        target(roles=("payroll_admin", "admin")),
        PatchRequest(roles=["payroll_admin", "approver", "approver"]),
        1,
    )
    assert change.old_roles == ["admin", "payroll_admin"]
    assert change.new_roles == ["approver", "payroll_admin"]


def test_roles_changed_compares_sets() -> None:
    """PAY-107: order and duplicates don't make a change."""
    assert not roles_changed(["admin", "approver"], ["approver", "admin", "admin"])
    assert roles_changed(["admin"], ["admin", "approver"])
