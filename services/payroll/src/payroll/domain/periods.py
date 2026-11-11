"""Pay periods (design §3.1; PAY-300, PAY-301, ADR 0006). Integer date arithmetic only."""

from __future__ import annotations

import calendar
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Final
from uuid import UUID

from payroll.domain import InvalidPeriodStart

__all__ = [
    "MAX_HOURS_HUNDREDTHS",
    "PERIODS_PER_YEAR",
    "Coverage",
    "Period",
    "Schedule",
    "biweekly_period_index",
    "derive_period",
    "find_overlapping_employees",
    "max_hours_hundredths",
    "periods_overlap",
]

BIWEEKLY_DAYS: Final = 14


class Schedule(StrEnum):
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"


PERIODS_PER_YEAR: Final[Mapping[Schedule, int]] = {Schedule.BIWEEKLY: 26, Schedule.MONTHLY: 12}
MAX_HOURS_HUNDREDTHS: Final[Mapping[Schedule, int]] = {
    Schedule.BIWEEKLY: 33_600,
    Schedule.MONTHLY: 74_400,
}


@dataclass(frozen=True, slots=True)
class Period:
    schedule: Schedule
    period_start: date
    period_end: date
    pay_date: date
    period_index: int
    periods_per_year: int


def biweekly_period_index(period_start: date, biweekly_anchor: date) -> int:
    """``((start - anchor).days // 14) mod 26 + 1`` (PAY-302); periodic in 26 periods."""
    delta = (period_start - biweekly_anchor).days
    return (delta // BIWEEKLY_DAYS) % PERIODS_PER_YEAR[Schedule.BIWEEKLY] + 1


def derive_period(schedule: Schedule, period_start: date, biweekly_anchor: date) -> Period:
    if schedule is Schedule.MONTHLY:
        if period_start.day != 1:
            raise InvalidPeriodStart(str(schedule), period_start.isoformat())
        last = calendar.monthrange(period_start.year, period_start.month)[1]
        end = date(period_start.year, period_start.month, last)
        index = period_start.month
    else:
        delta = (period_start - biweekly_anchor).days
        if delta % BIWEEKLY_DAYS != 0:
            raise InvalidPeriodStart(str(schedule), period_start.isoformat())
        end = period_start + timedelta(days=BIWEEKLY_DAYS - 1)
        index = biweekly_period_index(period_start, biweekly_anchor)
    return Period(
        schedule=schedule,
        period_start=period_start,
        period_end=end,
        pay_date=end,
        period_index=index,
        periods_per_year=PERIODS_PER_YEAR[schedule],
    )


def max_hours_hundredths(schedule: Schedule) -> int:
    return MAX_HOURS_HUNDREDTHS[schedule]


def periods_overlap(a_start: date, a_end: date, b_start: date, b_end: date) -> bool:
    """Inclusive date-range overlap."""
    return a_start <= b_end and b_start <= a_end


@dataclass(frozen=True, slots=True)
class Coverage:
    employee_id: UUID
    period_start: date
    period_end: date


def find_overlapping_employees(
    candidates: Iterable[UUID], existing: Iterable[Coverage], new_start: date, new_end: date
) -> list[UUID]:
    """Sorted, de-duplicated candidates with any coverage overlapping ``[new_start, new_end]``."""
    wanted = set(candidates)
    return sorted(
        {
            c.employee_id
            for c in existing
            if c.employee_id in wanted
            and periods_overlap(c.period_start, c.period_end, new_start, new_end)
        }
    )
