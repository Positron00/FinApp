"""bank-stub settings (design §4; C1, C47) and its extra startup check (§3.5; ADR 0001)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated, Final

from pycommon.settings import BaseServiceSettings, Env
from pycommon.startup_checks import CheckFailure, StartupCheck
from pydantic import Field, SecretStr

__all__ = ["STARTUP_CHECKS", "Settings", "check_not_prod"]

SERVICE_NAME: Final = "bank-stub"


class Settings(BaseServiceSettings):
    service_name: str = SERVICE_NAME
    db_user: str = "bank_stub_app"
    db_schema: str = "bank_stub"
    db_pool_size: int = 20
    db_max_overflow: int = 5
    # bank-stub has no Redis (use_redis=False); None also makes pycommon skip REDIS_PASSWORD.
    redis_host: str | None = None
    bank_stub_api_key: SecretStr
    bank_stub_failure_rate: Annotated[float, Field(ge=0.0, le=1.0)] = 0.05
    bank_stub_latency_ms: Annotated[int, Field(ge=0, le=10_000)] = 20


def check_not_prod(s: BaseServiceSettings) -> Iterable[CheckFailure]:
    """bank-stub is never deployed in prod (ADR 0001; C47 reason ``not_deployable``)."""
    if s.env is Env.prod:
        yield CheckFailure("ENV", "not_deployable")


STARTUP_CHECKS: Final[tuple[StartupCheck, ...]] = (check_not_prod,)
