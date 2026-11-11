"""Worked examples of the integer pay formulas (design §3.2; ADR 0006)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from payroll.domain import HoursMissing
from payroll.domain.calc import (
    PayslipInput,
    PayType,
    apply_bps,
    calculate_payslip,
    calculate_run,
    hourly_gross,
    run_totals,
    salaried_gross,
)


def _salaried(annual: int) -> PayslipInput:
    return PayslipInput(uuid4(), uuid4(), PayType.SALARIED, annual, None, None)


def _hourly(rate: int, hours: int | None) -> PayslipInput:
    return PayslipInput(uuid4(), uuid4(), PayType.HOURLY, None, rate, hours)


def test_salaried_leftover_cents_go_to_the_last_periods() -> None:
    """PAY-302: 5,200,001 over 26 → base 200,000, rem 1: only period 26 gets the extra cent."""
    grosses = [salaried_gross(5_200_001, k, 26) for k in range(1, 27)]
    assert grosses[:25] == [200_000] * 25
    assert grosses[25] == 200_001
    assert sum(grosses) == 5_200_001


def test_salaried_monthly_remainder_spread() -> None:
    """PAY-302: 100 cents over 12 → base 8, rem 4: periods 9-12 get 9."""
    assert [salaried_gross(100, k, 12) for k in range(1, 13)] == [8] * 8 + [9] * 4


@pytest.mark.parametrize(
    ("rate", "hours", "gross"),
    [
        (1_999, 150, 2_999),  # 2998.5 → 2999 (half up)
        (1_999, 149, 2_979),  # 2978.51 → 2979
        (1_000, 1, 10),
        (1, 49, 0),  # 0.49 → 0
        (1, 50, 1),  # 0.5 → 1
        (2_500, 0, 0),
    ],
)
def test_hourly_gross_rounds_half_up(rate: int, hours: int, gross: int) -> None:
    """PAY-302: (rate * hours_hundredths + 50) // 100."""
    assert hourly_gross(rate, hours) == gross


@pytest.mark.parametrize(
    ("amount", "bps", "expected"),
    [(1, 5000, 1), (3, 5000, 2), (1, 4999, 0), (10_001, 2000, 2_000), (5, 1000, 1), (4, 1250, 1)],
)
def test_apply_bps_half_up_boundaries(amount: int, bps: int, expected: int) -> None:
    """PAY-302: tax and retirement round half up at .5 cents."""
    assert apply_bps(amount, bps) == expected


def test_payslip_net_and_zero_hours() -> None:
    """PAY-302: net = gross - tax - retirement; hours 0 gives net 0."""
    a = calculate_payslip(
        _salaried(1_200_000), period_index=3, periods_per_year=12, tax_bps=2000, retirement_bps=500
    )
    assert (a.gross_cents, a.tax_cents, a.retirement_cents, a.net_cents) == (
        100_000,
        20_000,
        5_000,
        75_000,
    )
    z = calculate_payslip(
        _hourly(2_500, 0), period_index=1, periods_per_year=26, tax_bps=5000, retirement_bps=2000
    )
    assert z.net_cents == 0


def test_hours_missing_lists_every_missing_employee() -> None:
    """PAY-302: 422 hours_missing names every hourly employee without hours, before computing."""
    missing = [_hourly(1_000, None), _hourly(2_000, None)]
    inputs = [_salaried(100), *missing, _hourly(1_000, 800)]
    with pytest.raises(HoursMissing) as err:
        calculate_run(inputs, period_index=1, periods_per_year=12, tax_bps=0, retirement_bps=0)
    assert err.value.employee_ids == sorted(i.employee_id for i in missing)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"period_index": 0, "periods_per_year": 12, "tax_bps": 0, "retirement_bps": 0},
        {"period_index": 13, "periods_per_year": 12, "tax_bps": 0, "retirement_bps": 0},
        {"period_index": 1, "periods_per_year": 52, "tax_bps": 0, "retirement_bps": 0},
        {"period_index": 1, "periods_per_year": 12, "tax_bps": 5001, "retirement_bps": 0},
        {"period_index": 1, "periods_per_year": 12, "tax_bps": 0, "retirement_bps": 2001},
    ],
)
def test_preconditions_are_value_errors(kwargs: dict[str, int]) -> None:
    """PAY-302: out-of-range k, n or rates are programming errors (ValueError), not user errors."""
    with pytest.raises(ValueError, match=r"range|12 or 26"):
        calculate_payslip(_salaried(100), **kwargs)


def test_inconsistent_inputs_are_value_errors() -> None:
    """PAY-302: a salaried input without a salary or an hourly one without hours is a bug."""
    bad_salaried = PayslipInput(uuid4(), uuid4(), PayType.SALARIED, None, None, None)
    with pytest.raises(ValueError, match="salaried"):
        calculate_payslip(
            bad_salaried, period_index=1, periods_per_year=12, tax_bps=0, retirement_bps=0
        )
    with pytest.raises(ValueError, match="hourly"):
        calculate_payslip(
            _hourly(100, None), period_index=1, periods_per_year=12, tax_bps=0, retirement_bps=0
        )


def test_run_totals_sum_the_amounts() -> None:
    """PAY-302, PAY-303: totals are sums of the stored payslip values."""
    amounts = calculate_run(
        [_salaried(1_200_000), _hourly(1_999, 150)],
        period_index=1,
        periods_per_year=12,
        tax_bps=2000,
        retirement_bps=500,
    )
    t = run_totals(amounts.values())
    assert t.gross_cents == sum(a.gross_cents for a in amounts.values())
    assert t.net_cents == sum(a.net_cents for a in amounts.values())
    assert t.gross_cents == t.net_cents + t.tax_cents + t.retirement_cents
