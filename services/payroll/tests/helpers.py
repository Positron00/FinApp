"""Helpers shared by the payroll suites."""

from __future__ import annotations

import base64
import json
import os
import secrets
from typing import Any
from uuid import UUID

from payroll.settings import Settings
from pydantic import SecretStr

COMPANY_ID = UUID("0193a000-0000-7000-8000-000000000001")
OTHER_COMPANY_ID = UUID("0193a000-0000-7000-8000-000000000002")
STRONG_SECRET = secrets.token_urlsafe(24)  # generated per run: never a committed secret (C63)
ACTIVE_KID = "k2026a"
VALID_ROUTING = "021000021"  # passes the ABA checksum
OTHER_ROUTING = "011000015"


def field_key() -> bytes:
    return os.urandom(32)


def field_keys_json(keys: dict[str, bytes] | None = None) -> str:
    keys = keys or {ACTIVE_KID: field_key()}
    return json.dumps({k: base64.b64encode(v).decode() for k, v in keys.items()})


def make_settings(**overrides: Any) -> Settings:
    """Settings built without reading env or ``.env`` (``model_validate`` skips the sources)."""
    values: dict[str, Any] = {
        "company_id": COMPANY_ID,
        "db_password": SecretStr(STRONG_SECRET),
        "redis_password": SecretStr(STRONG_SECRET + "-redis"),
        "payroll_field_keys": SecretStr(field_keys_json()),
        "payroll_field_active_kid": ACTIVE_KID,
    }
    values.update(overrides)
    return Settings.model_validate(values)


def employee_body(
    name: str = "Ada Lovelace",
    email: str = "ada@example.com",
    *,
    pay_type: str = "salaried",
    annual_salary_cents: int | None = 5_200_001,
    hourly_rate_cents: int | None = None,
    schedule: str = "monthly",
    bank_routing: str = VALID_ROUTING,
    bank_account: str = "000123456789",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": name,
        "email": email,
        "pay_type": pay_type,
        "schedule": schedule,
        "bank_routing": bank_routing,
        "bank_account": bank_account,
    }
    if annual_salary_cents is not None:
        body["annual_salary_cents"] = annual_salary_cents
    if hourly_rate_cents is not None:
        body["hourly_rate_cents"] = hourly_rate_cents
    return body
