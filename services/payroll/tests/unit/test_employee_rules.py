"""Employee PATCH rules on the stored row (design §4.3; PAY-203, R34)."""

# ruff: noqa: E501  (one case per line in the parameter tables)

from __future__ import annotations

from typing import Any

import pytest
from payroll.domain import InvalidFields
from payroll.domain.employees import PayBasis, check_bank_completeness, merge_pay_basis

SALARIED = PayBasis("salaried", 5_200_000, None)
HOURLY = PayBasis("hourly", None, 2_500)


def _merge(current: PayBasis, **body: Any) -> PayBasis:
    return merge_pay_basis(
        current,
        supplied=set(body),
        pay_type=body.get("pay_type"),
        annual_salary_cents=body.get("annual_salary_cents"),
        hourly_rate_cents=body.get("hourly_rate_cents"),
    )


@pytest.mark.parametrize(
    ("current", "body", "expected"),
    [
        (SALARIED, {}, SALARIED),
        (SALARIED, {"annual_salary_cents": 6_000_000}, PayBasis("salaried", 6_000_000, None)),
        (SALARIED, {"pay_type": "salaried"}, SALARIED),
        (SALARIED, {"pay_type": "hourly", "hourly_rate_cents": 3_000}, PayBasis("hourly", None, 3_000)),
        (HOURLY, {"hourly_rate_cents": 2_600}, PayBasis("hourly", None, 2_600)),
        (HOURLY, {"pay_type": "salaried", "annual_salary_cents": 1}, PayBasis("salaried", 1, None)),
    ],
)  # fmt: skip
def test_merge_pay_basis(current: PayBasis, body: dict[str, Any], expected: PayBasis) -> None:
    """PAY-203: the merged basis keeps or replaces the amount, and clears the other type's."""
    assert _merge(current, **body) == expected


@pytest.mark.parametrize(
    ("current", "body", "issues"),
    [
        (SALARIED, {"pay_type": "hourly"}, [("body.hourly_rate_cents", "rate_required")]),
        (HOURLY, {"pay_type": "salaried"}, [("body.annual_salary_cents", "salary_required")]),
        (SALARIED, {"hourly_rate_cents": 100}, [("body.hourly_rate_cents", "pay_basis")]),
        (HOURLY, {"annual_salary_cents": 100}, [("body.annual_salary_cents", "pay_basis")]),
        (
            SALARIED,
            {"pay_type": "hourly", "annual_salary_cents": 100},
            [
                ("body.annual_salary_cents", "pay_basis"),
                ("body.hourly_rate_cents", "rate_required"),
            ],
        ),
    ],
)
def test_merge_pay_basis_refusals(
    current: PayBasis, body: dict[str, Any], issues: list[tuple[str, str]]
) -> None:
    """PAY-203, C14: changing the type without its amount, or sending the other type's amount,
    is 422 validation_error with field and issue only."""
    with pytest.raises(InvalidFields) as err:
        _merge(current, **body)
    assert err.value.issues == issues
    assert err.value.error_code == "validation_error"
    assert err.value.status == 422


def test_bank_completeness_with_details_on_file() -> None:
    """PAY-203: with bank details on file, either field may change alone."""
    for supplied in (set(), {"bank_routing"}, {"bank_account"}):
        check_bank_completeness(has_bank_details=True, supplied=supplied, reactivating=True)


def test_bank_completeness_after_purge() -> None:
    """PAY-203, PAY-608: without details (purged), reactivation needs both fields
    (required_on_reactivation); a single field alone is bank_details_incomplete; both pass."""
    both = {"bank_routing", "bank_account"}
    check_bank_completeness(has_bank_details=False, supplied=both, reactivating=True)
    check_bank_completeness(has_bank_details=False, supplied={"name"}, reactivating=False)
    with pytest.raises(InvalidFields) as err:
        check_bank_completeness(has_bank_details=False, supplied=set(), reactivating=True)
    assert err.value.issues == [
        ("body.bank_routing", "required_on_reactivation"),
        ("body.bank_account", "required_on_reactivation"),
    ]
    with pytest.raises(InvalidFields) as err:
        check_bank_completeness(
            has_bank_details=False, supplied={"bank_routing"}, reactivating=True
        )
    assert err.value.issues == [("body.bank_account", "required_on_reactivation")]
    with pytest.raises(InvalidFields) as err:
        check_bank_completeness(
            has_bank_details=False, supplied={"bank_account"}, reactivating=False
        )
    assert err.value.issues == [("body.bank_routing", "bank_details_incomplete")]
