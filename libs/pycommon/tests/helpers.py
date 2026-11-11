"""Helpers shared by the pycommon suites."""

from __future__ import annotations

import secrets
from typing import Any
from uuid import UUID

from pycommon.settings import BaseServiceSettings
from pydantic import SecretStr

COMPANY_ID = UUID("0193a000-0000-7000-8000-000000000001")
# Built at runtime (testing.md §9, C63): no credential-shaped literal in the repo.
STRONG_SECRET = secrets.token_urlsafe(24)


class SvcSettings(BaseServiceSettings):
    service_name: str = "testsvc"


def make_settings(**overrides: Any) -> SvcSettings:
    """Settings built without reading env or ``.env`` (``model_validate`` skips the sources)."""
    values: dict[str, Any] = {
        "company_id": COMPANY_ID,
        "db_user": "testsvc_app",
        "db_password": SecretStr(STRONG_SECRET),
        "db_schema": "testsvc",
        "redis_username": "testsvc",
        "redis_password": SecretStr(STRONG_SECRET + "-redis"),
    }
    values.update(overrides)
    return SvcSettings.model_validate(values)
