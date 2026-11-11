"""Base settings shared by every Python service (platform LLD §3.1, §4; C1, C24).

The field names below are read by their unprefixed env names (``DB_HOST``, ``REDIS_PASSWORD``, …)
in every service and environment. Services subclass :class:`BaseServiceSettings` and add their own
keys with a service prefix.
"""

from __future__ import annotations

import json
import sys
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID

from pydantic import BeforeValidator, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from pycommon.auth import Keyset

__all__ = [
    "BaseServiceSettings",
    "Env",
    "SettingsError",
    "load_settings",
    "normalise_pem",
]


class Env(StrEnum):
    dev = "dev"
    uat = "uat"
    prod = "prod"


class SettingsError(Exception):
    """Raised by ``load_settings``; the message lists field names and error types only."""

    def __init__(self, errors: list[tuple[str, str]] | None = None, message: str | None = None):
        self.errors: list[tuple[str, str]] = list(errors or [])
        if message is None:
            listed = ", ".join(f"{name} ({kind})" for name, kind in self.errors)
            message = f"invalid settings: {listed}" if listed else "invalid settings"
        super().__init__(message)

    @property
    def fields(self) -> list[str]:
        return [name for name, _ in self.errors]


def normalise_pem(value: str) -> str:
    """Turn literal ``\\n`` sequences into newlines and strip surrounding quotes and blanks."""
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in {"'", '"'}:
        v = v[1:-1]
    v = v.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\r\n", "\n")
    return v.strip() + "\n" if v.strip() else ""


def _parse_keyset(value: object) -> object:
    if value is None or isinstance(value, Keyset):
        return value
    if isinstance(value, str):
        if not value.strip():
            return None
        try:
            return Keyset.from_json(value)
        except SettingsError as exc:
            # Re-raised as a pydantic error so load_settings reports the field name only.
            raise ValueError("invalid keyset") from exc
    raise ValueError("invalid keyset")


KeysetField = Annotated[Keyset | None, BeforeValidator(_parse_keyset)]


class BaseServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        case_sensitive=False,
        validate_by_name=True,
        validate_by_alias=True,
        arbitrary_types_allowed=True,
    )

    service_name: str
    version: str = Field("0.0.0-dev", alias="APP_VERSION")
    env: Env = Env.dev
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    debug: bool = False
    docs_enabled: bool = False
    company_id: UUID
    # Postgres (C1: the same keys for runtime and migrate processes; no DATABASE_URL)
    db_host: str = "postgres"
    db_port: int = 5432
    db_name: str = "payrun"
    db_user: str
    db_password: SecretStr
    db_schema: str
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_statement_timeout_ms: int = 2000
    db_pool_timeout_s: float = 2.0
    # Redis (C1: no REDIS_URL; unused by bank-stub)
    redis_host: str | None = "redis"
    redis_port: int = 6379
    redis_username: str | None = None
    redis_password: SecretStr | None = None
    redis_socket_timeout_ms: int = 500
    # internal JWT (None for services without JWT-authenticated routes)
    internal_jwt_public_keys: KeysetField = None
    jwt_leeway_s: int = 5
    # limits and shared resilience
    max_body_bytes: int = 262_144
    service_rate_limit_user: int = 600
    service_rate_limit_worker: int = 6000
    breaker_failures: int = 5
    breaker_reset_s: float = 30.0
    maintenance_interval_s: int = 60
    # retention windows (PAY-608), read by each service's own purge tasks
    retention_outbox_days: int = 7
    retention_processed_events_days: int = 30
    retention_tokens_days: int = 7
    retention_bank_details_days: int = 30
    idempotency_ttl_h: int = 24

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip().upper()
        return v

    def db_url(self, *, user: str | None = None, password: SecretStr | None = None) -> str:
        """The asyncpg URL from DB_HOST/PORT/NAME/USER/PASSWORD, user and password quoted."""
        u = quote(user if user is not None else self.db_user, safe="")
        secret = password if password is not None else self.db_password
        p = quote(secret.get_secret_value(), safe="")
        db = quote(self.db_name, safe="")
        return f"postgresql+asyncpg://{u}:{p}@{self.db_host}:{self.db_port}/{db}"

    @classmethod
    def env_name(cls, field_name: str) -> str:
        """The env var a field is read from (its alias, else its name), upper-cased."""
        info = cls.model_fields[field_name]
        prefix = str(cls.model_config.get("env_prefix", "") or "")
        return (info.alias or f"{prefix}{field_name}").upper()

    def secret_fields(self) -> dict[str, SecretStr]:
        """Every set ``SecretStr`` field, keyed by its env name."""
        out: dict[str, SecretStr] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                out[type(self).env_name(name)] = value
        return out


def _error_name(cls: type[BaseServiceSettings], loc: tuple[int | str, ...]) -> str:
    if not loc:
        return "<root>"
    head = str(loc[0])
    if head in cls.model_fields:
        return cls.env_name(head)
    for name, info in cls.model_fields.items():
        if info.alias and info.alias.lower() == head.lower():
            return cls.env_name(name)
    return head.upper()


def load_settings[S: BaseServiceSettings](cls: type[S]) -> S:
    """Load settings from the environment; on invalid input log ``settings.invalid`` and exit 2.

    The raised ``SystemExit(2)`` carries the :class:`SettingsError` as its ``__cause__``.
    Neither the log line nor the error contains an input value (pydantic's ``input`` is
    dropped; C29).
    """
    try:
        return cls()
    except ValidationError as exc:
        errors = [(_error_name(cls, tuple(e["loc"])), str(e["type"])) for e in exc.errors()]
        err = SettingsError(errors)
    _report_invalid(err)
    raise SystemExit(2) from err


def _report_invalid(err: SettingsError) -> None:
    import logging  # noqa: PLC0415  (stdlib; pycommon.logging may not be configured yet)

    from pycommon.logging import get_logger, log_event  # noqa: PLC0415

    if logging.getLogger().handlers:
        log_event(
            get_logger("pycommon.settings"), logging.ERROR, "settings.invalid", fields=err.fields
        )
    else:
        line = {"level": "error", "event": "settings.invalid", "fields": err.fields}
        sys.stderr.write(json.dumps(line) + "\n")
