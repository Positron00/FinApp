"""payroll settings (design §6; C1) and its extra startup checks (§6.2; PAY-605, C29, C48)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated, Final

from pycommon.settings import BaseServiceSettings
from pycommon.startup_checks import (
    CheckFailure,
    StartupCheck,
    check_field_keys,
    check_mark_rejected_wait,
    check_republish_after,
)
from pydantic import Field, SecretStr

from payroll.crypto import FieldKeyring

__all__ = ["STARTUP_CHECKS", "Settings"]

SERVICE_NAME: Final = "payroll"
ACCEPTED_ISSUERS: Final = frozenset({"web", "payout-worker"})


class Settings(BaseServiceSettings):
    service_name: str = SERVICE_NAME
    db_user: str = "payroll_app"
    db_schema: str = "payroll"
    redis_username: str | None = "payroll"
    # stream and outbox relay (PAY-402)
    stream_maxlen: int = 100_000
    payroll_outbox_batch: Annotated[int, Field(ge=1, le=1000)] = 100
    payroll_outbox_poll_ms: Annotated[int, Field(ge=1)] = 200
    payroll_validate_events: bool = True
    # migration seed (PAY-200, R22)
    payroll_seed_company_name: str = "PayRun Demo Company"
    # field encryption (PAY-204, ADR 0004)
    payroll_field_keys: SecretStr
    payroll_field_active_kid: str
    # shared payout timing (R11; the payout-timing ConfigMap)
    payout_max_attempts: Annotated[int, Field(ge=1)] = 5
    payout_republish_after_s: int = 360
    payout_republish_max_backoff_s: int = 900
    payout_republish_max: int = 10
    payout_mark_rejected_min_wait_s: int = 120
    payroll_republish_batch: int = 1000
    payout_max_deliveries: int = 5
    payout_claim_min_idle_ms: int = 60_000
    bank_total_deadline_s: float = 15.0
    payout_payroll_call_deadline_s: float = 5.0
    payout_stuck_after_s: int = 600
    payroll_gauge_query_timeout_ms: int = 1000


def _s(s: BaseServiceSettings) -> Settings:
    if not isinstance(s, Settings):  # pragma: no cover  (pycommon passes our own settings)
        raise TypeError("payroll startup checks need payroll Settings")
    return s


def check_payroll_field_keys(base: BaseServiceSettings) -> Iterable[CheckFailure]:
    """pycommon's kid rules, plus: every key decodes to exactly 32 bytes (ADR 0004)."""
    s = _s(base)
    yield from check_field_keys(s.payroll_field_keys.get_secret_value(), s.payroll_field_active_kid)
    try:
        FieldKeyring.from_settings(s.payroll_field_keys, s.payroll_field_active_kid)
    except ValueError:
        yield CheckFailure("PAYROLL_FIELD_KEYS", "invalid_key")


def check_jwt_issuers(base: BaseServiceSettings) -> Iterable[CheckFailure]:
    """Every kid's issuer is ``web`` or ``payout-worker``, with at least one kid each (PAY-600,
    design §6.2 item 3)."""
    keyset = base.internal_jwt_public_keys
    if keyset is None:
        return  # pycommon's own JWT check reports "missing"
    issuers = {entry.iss for kid in keyset.kids if (entry := keyset.get(kid)) is not None}
    if issuers - ACCEPTED_ISSUERS:
        yield CheckFailure("INTERNAL_JWT_PUBLIC_KEYS", "issuer_unexpected")
    if not issuers >= ACCEPTED_ISSUERS:
        yield CheckFailure("INTERNAL_JWT_PUBLIC_KEYS", "issuer_missing")


def check_timing(base: BaseServiceSettings) -> Iterable[CheckFailure]:
    """R1/R11 re-publish timing, the payroll extras, and the C48 mark_rejected wait."""
    s = _s(base)
    yield from check_republish_after(
        s.payout_republish_after_s,
        s.payout_max_deliveries,
        s.payout_claim_min_idle_ms,
        s.bank_total_deadline_s,
    )
    if s.payout_republish_max_backoff_s < s.payout_republish_after_s:
        yield CheckFailure("PAYOUT_REPUBLISH_MAX_BACKOFF_S", "timing_invariant")
    yield from check_mark_rejected_wait(
        s.payout_mark_rejected_min_wait_s,
        s.bank_total_deadline_s,
        s.payout_payroll_call_deadline_s,
    )


STARTUP_CHECKS: Final[tuple[StartupCheck, ...]] = (
    check_payroll_field_keys,
    check_jwt_issuers,
    check_timing,
)
