"""``decide_after_failure`` and the consumer's action per result (design §3.2, §3.3.4; PAY-408,
C21)."""

from __future__ import annotations

from typing import Any

import pytest
from payout_worker.consumer import decide_after_failure

MAX = 5


@pytest.mark.parametrize("delivery", [1, 4, 5, 9])
def test_poison_dead_letters_at_once(delivery: int) -> None:
    """R5, PAY-408: invalid → DLQ at any delivery, including the first."""
    assert decide_after_failure("invalid", delivery, MAX) == "dlq"


@pytest.mark.parametrize("kind", ["payroll", "internal"])
@pytest.mark.parametrize(
    ("delivery", "expected"), [(1, "retry"), (4, "retry"), (5, "dlq"), (7, "dlq")]
)
def test_payroll_and_internal_at_the_budget(kind: Any, delivery: int, expected: str) -> None:
    """PAY-408: payroll and worker failures stay pending, then go to the DLQ at the budget."""
    assert decide_after_failure(kind, delivery, MAX) == expected


@pytest.mark.parametrize("delivery", [1, 5, 6])
def test_bank_never_dead_letters(delivery: int) -> None:
    """PAY-404: the bank row never goes to the DLQ (the handler posts indeterminate)."""
    assert decide_after_failure("bank", delivery, MAX) == "retry"
