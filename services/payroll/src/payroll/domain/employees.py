"""Employee PATCH rules that need the stored row (design §4.3; PAY-203, R34).

Pure functions: the service passes the stored values and the supplied fields, and gets either
the merged values or ``InvalidFields`` (422 ``validation_error`` with ``[{field, issue}]``).
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from payroll.domain import InvalidFields

__all__ = ["PayBasis", "check_bank_completeness", "merge_pay_basis"]

SALARY: str = "annual_salary_cents"
RATE: str = "hourly_rate_cents"


@dataclass(frozen=True, slots=True)
class PayBasis:
    pay_type: str
    annual_salary_cents: int | None
    hourly_rate_cents: int | None


def merge_pay_basis(
    current: PayBasis,
    *,
    supplied: Collection[str],
    pay_type: str | None,
    annual_salary_cents: int | None,
    hourly_rate_cents: int | None,
) -> PayBasis:
    """Merge the PATCH onto the stored pay basis (the ``ck_employees_pay_basis`` rule).

    - The amount for the merged ``pay_type`` comes from the body, or is kept from the row when the
      type doesn't change. Changing the type without the matching amount → ``salary_required`` or
      ``rate_required``.
    - Supplying the other type's amount → ``pay_basis``.
    - The other type's amount is cleared to NULL.
    """
    new_type = pay_type if "pay_type" in supplied and pay_type is not None else current.pay_type
    same_type = new_type == current.pay_type
    if new_type == "salaried":
        own, other, own_issue = SALARY, RATE, "salary_required"
        own_value = annual_salary_cents if SALARY in supplied else None
        kept = current.annual_salary_cents if same_type else None
    else:
        own, other, own_issue = RATE, SALARY, "rate_required"
        own_value = hourly_rate_cents if RATE in supplied else None
        kept = current.hourly_rate_cents if same_type else None
    issues: list[tuple[str, str]] = []
    if other in supplied:
        issues.append((f"body.{other}", "pay_basis"))
    amount = own_value if own_value is not None else kept
    if amount is None:
        issues.append((f"body.{own}", own_issue))
    if issues:
        raise InvalidFields(issues)
    if new_type == "salaried":
        return PayBasis(new_type, amount, None)
    return PayBasis(new_type, None, amount)


def check_bank_completeness(
    *, has_bank_details: bool, supplied: Collection[str], reactivating: bool
) -> None:
    """Bank columns are all-or-none, and an active employee needs them (design §2.2).

    With details on file, either field may change alone. Without them (purged after
    deactivation, PAY-608), both must come together, and reactivation needs both
    (``required_on_reactivation``, design §4.3 [decision]).
    """
    if has_bank_details:
        return
    missing = [f for f in ("bank_routing", "bank_account") if f not in supplied]
    if not missing:
        return
    if reactivating:
        raise InvalidFields((f"body.{f}", "required_on_reactivation") for f in missing)
    if len(missing) == 1:
        raise InvalidFields([(f"body.{missing[0]}", "bank_details_incomplete")])
