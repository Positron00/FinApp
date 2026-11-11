"""Period derivation and hours bounds (design §3.1)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from payroll.domain import InvalidPeriodStart
from payroll.domain.periods import (
    Schedule,
    biweekly_period_index,
    derive_period,
    max_hours_hundredths,
)

ANCHOR = date(2026, 1, 5)  # a Monday, the default anchor


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2026, 1, 1), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 2, 28)),
        (date(2028, 2, 1), date(2028, 2, 29)),  # leap year
        (date(2026, 4, 1), date(2026, 4, 30)),
        (date(2026, 12, 1), date(2026, 12, 31)),
    ],
)
def test_monthly_period_end_is_month_end(start: date, end: date) -> None:
    """PAY-300: monthly runs start on the 1st and end on the month's last day (incl. Feb 29)."""
    p = derive_period(Schedule.MONTHLY, start, ANCHOR)
    assert (p.period_end, p.pay_date) == (end, end)
    assert p.period_index == start.month
    assert p.periods_per_year == 12


@pytest.mark.parametrize("day", [2, 15, 28])
def test_monthly_rejects_a_start_that_is_not_the_first(day: int) -> None:
    """PAY-300: 422 invalid_period_start unless period_start.day == 1."""
    with pytest.raises(InvalidPeriodStart) as err:
        derive_period(Schedule.MONTHLY, date(2026, 3, day), ANCHOR)
    assert err.value.details == {"schedule": "monthly", "period_start": f"2026-03-{day:02d}"}


@pytest.mark.parametrize("k", [-27, -1, 0, 1, 25, 26, 27, 52])
def test_biweekly_periods_on_the_anchor_grid(k: int) -> None:
    """PAY-300: anchor ± 14k (before and after the anchor), end = start + 13, index wraps at 26."""
    start = ANCHOR + timedelta(days=14 * k)
    p = derive_period(Schedule.BIWEEKLY, start, ANCHOR)
    assert p.period_end == start + timedelta(days=13)
    assert p.pay_date == p.period_end
    assert p.period_index == k % 26 + 1
    assert p.periods_per_year == 26


@pytest.mark.parametrize("offset", [1, 7, 13, -1, -13])
def test_biweekly_rejects_off_grid_starts(offset: int) -> None:
    """PAY-300: a biweekly start not on the anchor + 14k grid is invalid_period_start."""
    with pytest.raises(InvalidPeriodStart):
        derive_period(Schedule.BIWEEKLY, ANCHOR + timedelta(days=offset), ANCHOR)


def test_biweekly_index_first_and_last() -> None:
    """PAY-302: the index of the anchor period is 1 and of the period before it is 26."""
    assert biweekly_period_index(ANCHOR, ANCHOR) == 1
    assert biweekly_period_index(ANCHOR - timedelta(days=14), ANCHOR) == 26


def test_hours_bounds_per_schedule() -> None:
    """PAY-301: 33,600 hundredths biweekly and 74,400 monthly."""
    assert max_hours_hundredths(Schedule.BIWEEKLY) == 33_600
    assert max_hours_hundredths(Schedule.MONTHLY) == 74_400
