"""payout-worker settings (design §4; C1, C46, C47) and its extra startup checks (§3.7; C29)."""

from __future__ import annotations

import socket
from collections.abc import Iterable
from typing import Annotated, Final, Self

from pycommon.settings import BaseServiceSettings, Env
from pycommon.startup_checks import (
    CheckFailure,
    StartupCheck,
    check_bank_base_url,
    check_claim_idle,
    check_republish_after,
)
from pydantic import Field, SecretStr, model_validator

__all__ = [
    "DLQ_MAXLEN",
    "STARTUP_CHECKS",
    "TERMINATION_GRACE_S",
    "Settings",
    "check_bank_url",
    "check_key_window_explicit",
    "check_timing",
]

SERVICE_NAME: Final = "payout-worker"
DLQ_MAXLEN: Final = 10_000  # C21: XADD … MAXLEN ~ 10000
TERMINATION_GRACE_S: Final = 75  # terminationGracePeriodSeconds (plan §2.2)


class Settings(BaseServiceSettings):
    service_name: str = SERVICE_NAME
    db_user: str = "payouts_app"
    db_schema: str = "payouts"
    db_pool_size: int = 20
    db_max_overflow: int = 5
    redis_username: str | None = "payout-worker"
    # stream and consumer (PAY-403, PAY-408)
    payout_xread_block_ms: Annotated[int, Field(ge=1)] = 5000
    payout_stream: str = "stream:payout.requested"
    payout_dlq_stream: str = "stream:payout.requested.dlq"
    payout_group: str = "payout-worker"
    payout_consumer_name: Annotated[str, Field(min_length=1, max_length=200)] = Field(
        default_factory=socket.gethostname
    )
    payout_concurrency: Annotated[int, Field(ge=1)] = 16
    payout_max_deliveries: Annotated[int, Field(ge=1)] = 5
    payout_claim_min_idle_ms: Annotated[int, Field(ge=1)] = 60_000
    payout_claim_interval_s: Annotated[float, Field(gt=0)] = 10.0
    payout_shutdown_drain_s: Annotated[float, Field(ge=0)] = 60.0
    payout_lag_poll_s: Annotated[float, Field(gt=0)] = 15.0
    payout_consumer_gc_idle_s: int = 3600  # read by prune_consumers (F4b, design §3.8)
    payout_republish_after_s: int = 360  # read only by the startup check (R11)
    # payroll client (C46)
    payout_payroll_base_url: str = "http://payroll:8000"
    payout_payroll_connect_timeout_s: float = 1.0
    payout_payroll_read_timeout_s: float = 2.0
    payout_payroll_max_retries: Annotated[int, Field(ge=0)] = 3
    payout_payroll_call_deadline_s: Annotated[float, Field(gt=0)] = 5.0
    # worker JWT (PAY-600)
    payout_worker_jwt_private_key: SecretStr
    payout_worker_jwt_kid: str
    payout_worker_jwt_ttl_s: Annotated[int, Field(ge=1, le=60)] = 60
    # bank (PAY-404, PAY-410, PAY-412)
    bank_base_url: str = "http://bank-stub:8000"
    bank_stub_api_key: SecretStr
    bank_connect_timeout_s: float = 1.0
    bank_read_timeout_s: float = 5.0
    bank_max_retries: Annotated[int, Field(ge=0)] = 3
    bank_total_deadline_s: Annotated[float, Field(gt=0)] = 15.0
    bank_key_safe_window_s: Annotated[int, Field(ge=1)] = 86_400

    @model_validator(mode="after")
    def _drain_fits_grace(self) -> Self:
        if self.payout_shutdown_drain_s + 10 > TERMINATION_GRACE_S:
            raise ValueError("PAYOUT_SHUTDOWN_DRAIN_S + 10 must be at most 75")
        return self

    @property
    def handler_deadline_s(self) -> float:
        """``BANK_TOTAL_DEADLINE_S + 2 * PAYOUT_PAYROLL_CALL_DEADLINE_S + 10`` (35 s; C48)."""
        return self.bank_total_deadline_s + 2 * self.payout_payroll_call_deadline_s + 10


def _s(s: BaseServiceSettings) -> Settings:
    if not isinstance(s, Settings):  # pragma: no cover  (pycommon passes our own settings)
        raise TypeError("payout-worker startup checks need payout-worker Settings")
    return s


def check_timing(base: BaseServiceSettings) -> Iterable[CheckFailure]:
    """Claim idle > handler deadline; re-publish > the delivery budget (plan §2.2, R1, R11)."""
    s = _s(base)
    yield from check_claim_idle(
        s.payout_claim_min_idle_ms, s.bank_total_deadline_s, s.payout_payroll_call_deadline_s
    )
    yield from check_republish_after(
        s.payout_republish_after_s,
        s.payout_max_deliveries,
        s.payout_claim_min_idle_ms,
        s.bank_total_deadline_s,
    )


def check_bank_url(base: BaseServiceSettings) -> Iterable[CheckFailure]:
    """Prod: BANK_BASE_URL isn't bank-stub, localhost or a placeholder (reason bank_stub_url)."""
    s = _s(base)
    yield from check_bank_base_url(s.bank_base_url, env=s.env)


def check_key_window_explicit(base: BaseServiceSettings) -> Iterable[CheckFailure]:
    """Prod: BANK_KEY_SAFE_WINDOW_S must be set, not the code default (PAY-410)."""
    s = _s(base)
    if s.env is Env.prod and "bank_key_safe_window_s" not in s.model_fields_set:
        yield CheckFailure("BANK_KEY_SAFE_WINDOW_S", "missing")


STARTUP_CHECKS: Final[tuple[StartupCheck, ...]] = (
    check_timing,
    check_bank_url,
    check_key_window_explicit,
)
