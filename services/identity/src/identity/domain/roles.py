"""Roles (ADR 0002; PAY-101, PAY-108)."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Final

__all__ = ["ALL_ROLES", "Role", "normalise_roles"]


class Role(StrEnum):
    admin = "admin"
    payroll_admin = "payroll_admin"
    approver = "approver"


ALL_ROLES: Final[tuple[Role, ...]] = tuple(sorted(Role))


def normalise_roles(roles: Iterable[str]) -> list[str]:
    """Sorted and de-duplicated, as stored in ``users.roles`` (§2.2)."""
    return sorted({str(Role(r)) for r in roles})
