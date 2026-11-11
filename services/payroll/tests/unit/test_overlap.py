"""Overlap rules (design §3.1). The service-side check arrives with F3; the rule is unit-tested."""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from payroll.domain.periods import Coverage, find_overlapping_employees, periods_overlap


def test_periods_overlap_is_inclusive() -> None:
    """PAY-300: shared end days overlap; adjacent days don't."""
    jan = (date(2026, 1, 1), date(2026, 1, 31))
    assert periods_overlap(*jan, date(2026, 1, 31), date(2026, 2, 13))
    assert periods_overlap(*jan, date(2025, 12, 20), date(2026, 1, 1))
    assert not periods_overlap(*jan, date(2026, 2, 1), date(2026, 2, 14))
    assert not periods_overlap(*jan, date(2025, 12, 18), date(2025, 12, 31))


def test_find_overlapping_employees_sorts_dedupes_and_filters() -> None:
    """PAY-300: a monthly run vs biweekly coverage; only candidates; sorted and de-duplicated."""
    a, b, c, outsider = (uuid4() for _ in range(4))
    existing = [
        Coverage(b, date(2026, 1, 19), date(2026, 2, 1)),
        Coverage(b, date(2026, 1, 5), date(2026, 1, 18)),
        Coverage(a, date(2026, 1, 5), date(2026, 1, 18)),
        Coverage(c, date(2026, 2, 2), date(2026, 2, 15)),  # adjacent: no overlap
        Coverage(outsider, date(2026, 1, 5), date(2026, 1, 18)),
    ]
    hits = find_overlapping_employees([a, b, c], existing, date(2026, 1, 1), date(2026, 1, 31))
    assert hits == sorted([a, b])
