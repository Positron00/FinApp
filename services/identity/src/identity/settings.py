"""Identity settings (identity LLD §4; C1, C49).

``IdentitySettings`` inherits the pycommon keys unchanged and adds the identity keys of §4.2, each
field named exactly as its env var. ``CoreSettings`` holds only what the ``bootstrap_owner`` CLI
needs (it runs in a container that has the migrator's ``DB_*`` keys but not the service secrets).
"""

from __future__ import annotations

from pycommon.settings import BaseServiceSettings
from pydantic import SecretStr, field_validator

__all__ = ["CoreSettings", "IdentitySettings", "Settings"]


class CoreSettings(BaseServiceSettings):
    """pycommon keys plus the argon2 parameters (shared by the service and the bootstrap CLI)."""

    service_name: str = "identity"
    db_schema: str = "identity"
    redis_username: str | None = "identity"
    identity_argon2_time_cost: int = 3
    identity_argon2_memory_kib: int = 65536
    identity_argon2_parallelism: int = 4
    identity_argon2_max_concurrency: int = 4


class IdentitySettings(CoreSettings):
    audit_ip_hash_key: SecretStr
    invite_ttl_h: int = 72
    reset_ttl_h: int = 1
    # Sign-in limits and lockout (§6.4); the defaults are the production values.
    auth_login_limit_account: int = 5
    auth_login_limit_ip: int = 20
    identity_login_window_s: int = 900
    identity_lockout_base_s: int = 60
    identity_lockout_max_s: int = 3600
    identity_lockout_level_ttl_s: int = 86400
    # Email (§3.9, §6.9)
    identity_public_base_url: str
    identity_email_from: str = "PayRun <no-reply@payrun.example.com>"
    smtp_host: str = "mailpit"
    smtp_port: int = 1025
    smtp_starttls: bool = False
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    identity_smtp_timeout_s: float = 5.0
    email_max_attempts: int = 10
    identity_email_retry_base_s: int = 30
    identity_email_retry_max_s: int = 1800
    identity_email_poll_interval_s: float = 2.0
    identity_email_batch: int = 10
    identity_email_lease_s: int = 60
    identity_retention_audit_days: int = 400

    @field_validator("identity_public_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.strip().rstrip("/")


# The name ``scripts/export_openapi.py`` imports.
Settings = IdentitySettings
