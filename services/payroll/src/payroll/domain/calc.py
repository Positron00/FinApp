"""Pay calculation in integer cents (design §3.2; PAY-302, PAY-NFR-01, ADR 0006).

No floats and no true division: every rounding is an explicit integer half-up.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

from payroll.domain import HoursMissing

__all__ = [
    "MAX_RETIREMENT_BPS",
    "MAX_TAX_BPS",
    "PayType",
    "PayslipAmounts",
    "PayslipInput",
    "RunTotals",
    "apply_bps",
    "calculate_payslip",
    "calculate_run",
    "hourly_gross",
    "run_totals",
    "salaried_gross",
]

MAX_TAX_BPS: Final = 5000
MAX_RETIREMENT_BPS: Final = 2000
BPS_DENOMINATOR: Final = 10_000
HUNDREDTHS: Final = 100
VALID_PERIODS: Final = frozenset({12, 26})


class PayType(StrEnum):
    SALARIED = "salaried"
    HOURLY = "hourly"


def salaried_gross(annual_cents: int, k: int, n: int) -> int:
    """Period ``k`` of ``n``: the leftover cents go to the last ``annual % n`` periods."""
    base, rem = divmod(annual_cents, n)
    return base + (1 if k > n - rem else 0)


def hourly_gross(rate_cents: int, hours_hundredths: int) -> int:
    """``(rate * hours_hundredths + 50) // 100``: half up to the cent."""
    return (rate_cents * hours_hundredths + HUNDREDTHS // 2) // HUNDREDTHS


def apply_bps(amount_cents: int, bps: int) -> int:
    """``(amount * bps + 5000) // 10000``: half up to the cent (tax and retirement)."""
    return (amount_cents * bps + BPS_DENOMINATOR // 2) // BPS_DENOMINATOR


@dataclass(frozen=True, slots=True)
class PayslipInput:
    payslip_id: UUID
    employee_id: UUID
    pay_type: PayType
    annual_salary_cents: int | None
    hourly_rate_cents: int | None
    hours_hundredths: int | None  # None = no hours entered


@dataclass(frozen=True, slots=True)
class PayslipAmounts:
    gross_cents: int
    tax_cents: int
    retirement_cents: int
    net_cents: int


@dataclass(frozen=True, slots=True)
class RunTotals:
    gross_cents: int
    tax_cents: int
    retirement_cents: int
    net_cents: int  # funding_required_cents == net_cents


def _check_preconditions(
    *, period_index: int, periods_per_year: int, tax_bps: int, retirement_bps: int
) -> None:
    if periods_per_year not in VALID_PERIODS:
        raise ValueError("periods_per_year must be 12 or 26")
    if not 1 <= period_index <= periods_per_year:
        raise ValueError("period_index out of range")
    if not 0 <= tax_bps <= MAX_TAX_BPS:
        raise ValueError("tax_bps out of range")
    if not 0 <= retirement_bps <= MAX_RETIREMENT_BPS:
        raise ValueError("retirement_bps out of range")


def _gross(inp: PayslipInput, *, period_index: int, periods_per_year: int) -> int:
    if inp.pay_type is PayType.SALARIED:
        if inp.annual_salary_cents is None:
            raise ValueError("a salaried payslip needs annual_salary_cents")
        return salaried_gross(inp.annual_salary_cents, period_index, periods_per_year)
    if inp.hourly_rate_cents is None or inp.hours_hundredths is None:
        raise ValueError("an hourly payslip needs a rate and hours")
    return hourly_gross(inp.hourly_rate_cents, inp.hours_hundredths)


def calculate_payslip(
    inp: PayslipInput,
    *,
    period_index: int,
    periods_per_year: int,
    tax_bps: int,
    retirement_bps: int,
) -> PayslipAmounts:
    _check_preconditions(
        period_index=period_index,
        periods_per_year=periods_per_year,
        tax_bps=tax_bps,
        retirement_bps=retirement_bps,
    )
    gross = _gross(inp, period_index=period_index, periods_per_year=periods_per_year)
    tax = apply_bps(gross, tax_bps)
    retirement = apply_bps(gross, retirement_bps)
    return PayslipAmounts(
        gross_cents=gross,
        tax_cents=tax,
        retirement_cents=retirement,
        net_cents=gross - tax - retirement,
    )


def calculate_run(
    inputs: Sequence[PayslipInput],
    *,
    period_index: int,
    periods_per_year: int,
    tax_bps: int,
    retirement_bps: int,
) -> dict[UUID, PayslipAmounts]:
    """Amounts per payslip ID; ``HoursMissing`` lists every hourly employee without hours first."""
    missing = [
        i.employee_id for i in inputs if i.pay_type is PayType.HOURLY and i.hours_hundredths is None
    ]
    if missing:
        raise HoursMissing(missing)
    return {
        i.payslip_id: calculate_payslip(
            i,
            period_index=period_index,
            periods_per_year=periods_per_year,
            tax_bps=tax_bps,
            retirement_bps=retirement_bps,
        )
        for i in inputs
    }


def run_totals(amounts: Iterable[PayslipAmounts]) -> RunTotals:
    """Sums of the stored payslip values (never recomputed from rates)."""
    gross = tax = retirement = net = 0
    for a in amounts:
        gross += a.gross_cents
        tax += a.tax_cents
        retirement += a.retirement_cents
        net += a.net_cents
    return RunTotals(gross_cents=gross, tax_cents=tax, retirement_cents=retirement, net_cents=net)
