"""Helpers shared by the identity suites."""

from __future__ import annotations

import json
from typing import Any, Final
from uuid import UUID

from identity.settings import IdentitySettings
from pycommon.testing import TokenFactory
from pydantic import SecretStr

COMPANY_ID: Final = UUID("0193a000-0000-7000-8000-000000000001")
AUDIT_KEY: Final = "k3y-for-ip-hashes-in-tests-0123456789abcdef"
BASE_URL: Final = "http://127.0.0.1:4100"
STRONG_SECRET: Final = "s3cr3t-Value-For-Tests-0123456789"
# Cheap argon2 for most tests; the timing and lockout tests use the production parameters.
FAST_ARGON2: Final[dict[str, int]] = {
    "identity_argon2_time_cost": 1,
    "identity_argon2_memory_kib": 1024,
    "identity_argon2_parallelism": 1,
}
PRODUCTION_ARGON2: Final[dict[str, int]] = {
    "identity_argon2_time_cost": 3,
    "identity_argon2_memory_kib": 65536,
    "identity_argon2_parallelism": 4,
}


def web_only_keyset(tf: TokenFactory) -> str:
    """identity's keyset holds web keys only (§3.1)."""
    doc = json.loads(tf.keyset_json)
    return json.dumps({k: v for k, v in doc.items() if v["iss"] == "web"})


def make_settings(**overrides: Any) -> IdentitySettings:
    """Settings built without reading env or ``.env`` (``model_validate`` skips the sources)."""
    values: dict[str, Any] = {
        "company_id": COMPANY_ID,
        "db_user": "identity_app",
        "db_password": SecretStr(STRONG_SECRET),
        "redis_password": SecretStr(STRONG_SECRET + "-redis"),
        "audit_ip_hash_key": SecretStr(AUDIT_KEY),
        "identity_public_base_url": BASE_URL,
        **FAST_ARGON2,
    }
    values.update(overrides)
    return IdentitySettings.model_validate(values)
