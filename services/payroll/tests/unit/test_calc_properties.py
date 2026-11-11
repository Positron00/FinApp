"""Hypothesis properties of the money rules (PAY-NFR-01 a, b, c; design §9.1)."""

from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

from hypothesis import given
from hypothesis import strategies as st
from payroll.domain.calc import (
    PayslipInput,
    PayType,
    calculate_payslip,
    calculate_run,
    run_totals,
    salaried_gross,
)
from payroll.domain.periods import biweekly_period_index

salaries = st.integers(min_value=1, max_value=1_000_000_000)
rates = st.integers(min_value=1, max_value=100_000)
hours = st.integers(min_value=0, max_value=74_400)
tax = st.integers(min_value=0, max_value=5000)
retirement = st.integers(min_value=0, max_value=2000)
schedules = st.sampled_from([12, 26])


@st.composite
def payslip_inputs(draw: st.DrawFn) -> PayslipInput:
    if draw(st.booleans()):
        return PayslipInput(uuid4(), uuid4(), PayType.SALARIED, draw(salaries), None, None)
    return PayslipInput(uuid4(), uuid4(), PayType.HOURLY, None, draw(rates), draw(hours))


@given(inp=payslip_inputs(), n=schedules, data=st.data(), tax_bps=tax, ret_bps=retirement)
def test_a_gross_equals_net_plus_deductions_and_net_is_non_negative(
    inp: PayslipInput, n: int, data: st.DataObject, tax_bps: int, ret_bps: int
) -> None:
    """PAY-NFR-01 (a): per payslip, gross = net + tax + retirement and net ≥ 0."""
    k = data.draw(st.integers(min_value=1, max_value=n))
    a = calculate_payslip(
        inp, period_index=k, periods_per_year=n, tax_bps=tax_bps, retirement_bps=ret_bps
    )
    assert a.gross_cents == a.net_cents + a.tax_cents + a.retirement_cents
    assert a.net_cents >= 0
    assert min(a.gross_cents, a.tax_cents, a.retirement_cents) >= 0


@given(annual=salaries, n=schedules)
def test_b_salaried_periods_sum_to_the_annual_salary(annual: int, n: int) -> None:
    """PAY-NFR-01 (b): Σ salaried_gross(annual, k, n) over k = 1..n == annual (n ∈ {12, 26})."""
    grosses = [salaried_gross(annual, k, n) for k in range(1, n + 1)]
    assert sum(grosses) == annual
    assert max(grosses) - min(grosses) <= 1


@given(
    inputs=st.lists(payslip_inputs(), min_size=1, max_size=30),
    n=schedules,
    data=st.data(),
    tax_bps=tax,
    ret_bps=retirement,
)
def test_c_run_funding_total_is_the_sum_of_net(
    inputs: list[PayslipInput], n: int, data: st.DataObject, tax_bps: int, ret_bps: int
) -> None:
    """PAY-NFR-01 (c): run_totals(...).net_cents == Σ net, and the gross total == Σ gross."""
    k = data.draw(st.integers(min_value=1, max_value=n))
    amounts = calculate_run(
        inputs, period_index=k, periods_per_year=n, tax_bps=tax_bps, retirement_bps=ret_bps
    )
    totals = run_totals(amounts.values())
    assert totals.net_cents == sum(a.net_cents for a in amounts.values())
    assert totals.gross_cents == sum(a.gross_cents for a in amounts.values())
    assert totals.gross_cents == totals.net_cents + totals.tax_cents + totals.retirement_cents


@given(offset=st.integers(min_value=-2000, max_value=2000))
def test_biweekly_period_index_is_periodic_in_26(offset: int) -> None:
    """PAY-NFR-01, PAY-302: the biweekly index repeats every 26 periods and stays in 1..26."""
    anchor = date(2026, 1, 5)
    start = anchor + timedelta(days=14 * offset)
    index = biweekly_period_index(start, anchor)
    assert 1 <= index <= 26
    assert biweekly_period_index(start + timedelta(days=14 * 26), anchor) == index
