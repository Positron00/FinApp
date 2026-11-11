"""HTTP layer. ``routers`` is the list ``main.build_app`` mounts (design §1)."""

from __future__ import annotations

from fastapi import APIRouter

__all__ = ["all_routers"]


def all_routers() -> list[APIRouter]:
    from payroll.api.audit_router import router as audit  # noqa: PLC0415
    from payroll.api.employees_router import router as employees  # noqa: PLC0415
    from payroll.api.internal_router import router as internal  # noqa: PLC0415
    from payroll.api.pay_runs_router import router as pay_runs  # noqa: PLC0415
    from payroll.api.settings_router import router as settings  # noqa: PLC0415

    return [settings, employees, pay_runs, audit, internal]
