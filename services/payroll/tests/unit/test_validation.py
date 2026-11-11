"""Input validation (PAY-200, PAY-201, PAY-405, PAY-602; C14, C26)."""

# ruff: noqa: E501  (one case per line in the parameter tables)

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from payroll.api.schemas import CompanySettingsIn, EmployeeCreate, ResultIn
from payroll.domain.validation import aba_checksum_ok, is_monday, is_valid_timezone
from pydantic import ValidationError

from tests.helpers import VALID_ROUTING, employee_body

SETTINGS: dict[str, Any] = {
    "name": "Acme",
    "tax_rate_bps": 2000,
    "retirement_bps": 500,
    "timezone": "America/New_York",
    "biweekly_anchor": "2026-01-05",
    "require_dual_approval": True,
}


def _issues(exc: ValidationError) -> set[str]:
    return {str(e["type"]) for e in exc.errors()}


@pytest.mark.parametrize(
    ("routing", "ok"),
    [(VALID_ROUTING, True), ("011000015", True), ("021000022", False), ("12345678", False)],
)
def test_aba_checksum(routing: str, ok: bool) -> None:
    """PAY-201: the routing number passes the ABA checksum."""
    assert aba_checksum_ok(routing) is ok


def test_timezone_and_monday_helpers() -> None:
    """PAY-200: IANA zones and Monday anchors."""
    assert is_valid_timezone("Europe/Berlin")
    assert not is_valid_timezone("Mars/Base")
    assert not is_valid_timezone("../etc/passwd")
    assert is_monday(date(2026, 1, 5))
    assert not is_monday(date(2026, 1, 6))


def test_settings_valid() -> None:
    """PAY-200: an in-range body parses."""
    assert CompanySettingsIn.model_validate(SETTINGS).tax_rate_bps == 2000


@pytest.mark.parametrize(
    ("field", "value", "issue"),
    [
        ("tax_rate_bps", 5001, "less_than_equal"),
        ("tax_rate_bps", -1, "greater_than_equal"),
        ("retirement_bps", 2001, "less_than_equal"),
        ("name", "", "string_too_short"),
        ("name", "x" * 201, "string_too_long"),
        ("timezone", "Not/AZone", "timezone"),
        ("biweekly_anchor", "2026-01-06", "not_monday"),
        ("extra", 1, "extra_forbidden"),
    ],
)
def test_settings_out_of_range(field: str, value: Any, issue: str) -> None:
    """PAY-200: out-of-range values are 422 with a pydantic or payroll issue code."""
    with pytest.raises(ValidationError) as err:
        CompanySettingsIn.model_validate({**SETTINGS, field: value})
    assert issue in _issues(err.value)


def test_employee_valid_bodies() -> None:
    """PAY-201: salaried and hourly bodies parse; the email is normalised by EmailStr."""
    EmployeeCreate.model_validate(employee_body())
    hourly = employee_body(pay_type="hourly", annual_salary_cents=None, hourly_rate_cents=2500)
    assert EmployeeCreate.model_validate(hourly).hourly_rate_cents == 2500


@pytest.mark.parametrize(
    ("overrides", "issue"),
    [
        ({"bank_routing": "021000022"}, "aba_checksum"),
        ({"bank_routing": "02100002"}, "string_pattern_mismatch"),
        ({"bank_account": "123"}, "string_pattern_mismatch"),
        ({"bank_account": "1" * 18}, "string_pattern_mismatch"),
        ({"annual_salary_cents": 0}, "greater_than_equal"),
        ({"annual_salary_cents": 1_000_000_001}, "less_than_equal"),
        ({"annual_salary_cents": None}, "salary_required"),
        ({"hourly_rate_cents": 100}, "pay_basis"),
        ({"pay_type": "hourly", "annual_salary_cents": None}, "rate_required"),
        ({"pay_type": "hourly", "hourly_rate_cents": 100}, "pay_basis"),
        (
            {"pay_type": "hourly", "annual_salary_cents": None, "hourly_rate_cents": 100_001},
            "less_than_equal",
        ),
        ({"email": "not-an-email"}, "value_error"),
        ({"schedule": "weekly"}, "literal_error"),
    ],
)
def test_employee_invalid(overrides: dict[str, Any], issue: str) -> None:
    """PAY-201: ABA checksum, account 4-17 digits, salary and rate bounds, pay basis rules."""
    body = {**employee_body(), **overrides}
    body = {k: v for k, v in body.items() if v is not None}
    with pytest.raises(ValidationError) as err:
        EmployeeCreate.model_validate(body)
    assert issue in _issues(err.value)


@pytest.mark.parametrize(
    "body",
    [
        {"attempt": 1, "outcome": "settled", "bank_reference": "ref-1"},
        {"attempt": 1, "outcome": "rejected", "reason": "account_closed"},
        {"attempt": 2, "outcome": "indeterminate"},
        {"attempt": 1, "outcome": "poison"},
        {"attempt": 1, "outcome": "poison", "reason": "amount_mismatch"},
        {"attempt": 1, "outcome": "settled", "bank_reference": "r" * 64},
    ],
)
def test_result_valid(body: dict[str, Any]) -> None:
    """PAY-405: the per-outcome field rules accept these."""
    ResultIn.model_validate(body)


@pytest.mark.parametrize(
    ("body", "issue"),
    [
        ({"attempt": 1, "outcome": "settled"}, "bank_reference_required"),
        ({"attempt": 1, "outcome": "settled", "bank_reference": "r" * 65}, "string_too_long"),
        ({"attempt": 1, "outcome": "settled", "bank_reference": "r", "reason": "x"}, "reason_not_allowed"),
        ({"attempt": 1, "outcome": "rejected"}, "reason_required"),
        ({"attempt": 1, "outcome": "rejected", "reason": "x", "bank_reference": "r"}, "bank_reference_not_allowed"),
        ({"attempt": 1, "outcome": "indeterminate", "reason": "x"}, "fields_not_allowed"),
        ({"attempt": 1, "outcome": "poison", "bank_reference": "r"}, "bank_reference_not_allowed"),
        ({"attempt": 1, "outcome": "rejected", "reason": "Account Closed"}, "string_pattern_mismatch"),
        ({"attempt": 0, "outcome": "indeterminate"}, "greater_than_equal"),
        ({"attempt": 1, "outcome": "lost"}, "literal_error"),
    ],
)  # fmt: skip
def test_result_invalid(body: dict[str, Any], issue: str) -> None:
    """PAY-405, C26: per-outcome rules; bank_reference ≤ 64; reason matches ^[a-z0-9_]{1,100}$."""
    with pytest.raises(ValidationError) as err:
        ResultIn.model_validate(body)
    assert issue in _issues(err.value)


@pytest.mark.parametrize(
    ("model", "body"),
    [
        (EmployeeCreate, employee_body(name="Ada\x00Lovelace")),
        (CompanySettingsIn, {**SETTINGS, "name": "Ac\x00me"}),
        (CompanySettingsIn, {**SETTINGS, "timezone": "America/New\x00York"}),
        (ResultIn, {"attempt": 1, "outcome": "settled", "bank_reference": "BR\x001"}),
    ],
    ids=["employee.name", "settings.name", "settings.timezone", "result.bank_reference"],
)
def test_nul_character_is_a_validation_error(model: Any, body: dict[str, Any]) -> None:
    """PAY-602, C63: a NUL in a free-text field is 422 nul_character (Postgres text can't hold
    it, so it would otherwise be a 500); the value is never echoed."""
    with pytest.raises(ValidationError) as info:
        model.model_validate(body)
    assert "nul_character" in _issues(info.value)
    assert "\x00" not in str([e.get("msg") for e in info.value.errors()])


def test_result_attempt_fits_smallint() -> None:
    """PAY-405, C63: attempt ≤ 32767 (payout_attempts.attempt is smallint), so it's 422, not 500."""
    assert ResultIn.model_validate({"attempt": 32767, "outcome": "indeterminate"}).attempt == 32767
    with pytest.raises(ValidationError) as info:
        ResultIn.model_validate({"attempt": 32768, "outcome": "indeterminate"})
    assert _issues(info.value) == {"less_than_equal"}
