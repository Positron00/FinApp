"""Identity startup checks (identity LLD §4.3; PAY-605, C29). Run with pycommon's common checks."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from pycommon.settings import BaseServiceSettings, Env
from pycommon.startup_checks import DEV_KID_PREFIXES, CheckFailure, StartupCheck

from identity.settings import IdentitySettings

__all__ = [
    "IDENTITY_CHECKS",
    "MIN_IP_HASH_KEY_BYTES",
    "check_audit_key",
    "check_base_url",
    "check_limits",
    "check_smtp",
    "check_web_issuer",
]

MIN_IP_HASH_KEY_BYTES: Final = 32
PROD_LIMIT_ACCOUNT: Final = 5
PROD_LIMIT_IP: Final = 20
PROD_LOCKOUT_BASE_S: Final = 60


def _identity(s: BaseServiceSettings) -> IdentitySettings:
    if not isinstance(s, IdentitySettings):  # pragma: no cover  (wiring error)
        raise TypeError("identity checks need IdentitySettings")
    return s


def check_web_issuer(s: BaseServiceSettings) -> Iterable[CheckFailure]:
    """Every kid in ``INTERNAL_JWT_PUBLIC_KEYS`` is bound to ``iss = "web"`` (§3.1)."""
    keyset = s.internal_jwt_public_keys
    if keyset is None:
        return
    for kid in keyset.kids:
        entry = keyset.get(kid)
        if entry is not None and entry.iss != "web":
            yield CheckFailure("INTERNAL_JWT_PUBLIC_KEYS", "wrong_issuer")
            return


def check_audit_key(s: BaseServiceSettings) -> Iterable[CheckFailure]:
    raw = _identity(s).audit_ip_hash_key.get_secret_value()
    if len(raw.encode()) < MIN_IP_HASH_KEY_BYTES:
        yield CheckFailure("AUDIT_IP_HASH_KEY", "too_short")
    if raw.lower().startswith(DEV_KID_PREFIXES):
        yield CheckFailure("AUDIT_IP_HASH_KEY", "dev_default")


def check_base_url(s: BaseServiceSettings) -> Iterable[CheckFailure]:
    if not _identity(s).identity_public_base_url.startswith("https://"):
        yield CheckFailure("IDENTITY_PUBLIC_BASE_URL", "not_https")


def check_smtp(s: BaseServiceSettings) -> Iterable[CheckFailure]:
    """Prod only: STARTTLS on, and SMTP credentials set."""
    i = _identity(s)
    if i.env is not Env.prod:
        return
    if not i.smtp_starttls:
        yield CheckFailure("SMTP_STARTTLS", "missing")
    if not i.smtp_username:
        yield CheckFailure("SMTP_USERNAME", "missing")
    if i.smtp_password is None or not i.smtp_password.get_secret_value():
        yield CheckFailure("SMTP_PASSWORD", "missing")


def check_limits(s: BaseServiceSettings) -> Iterable[CheckFailure]:
    """The raised test values can't reach uat or prod."""
    i = _identity(s)
    if i.auth_login_limit_account > PROD_LIMIT_ACCOUNT:
        yield CheckFailure("AUTH_LOGIN_LIMIT_ACCOUNT", "test_value")
    if i.auth_login_limit_ip > PROD_LIMIT_IP:
        yield CheckFailure("AUTH_LOGIN_LIMIT_IP", "test_value")
    if i.identity_lockout_base_s < PROD_LOCKOUT_BASE_S:
        yield CheckFailure("IDENTITY_LOCKOUT_BASE_S", "test_value")


IDENTITY_CHECKS: Final[tuple[StartupCheck, ...]] = (
    check_web_issuer,
    check_audit_key,
    check_base_url,
    check_smtp,
    check_limits,
)
