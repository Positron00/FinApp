"""Startup checks (platform LLD §3.12; PAY-605, C29, C48).

In uat and prod any failure logs ``startup.check_failed`` {setting, reason} at error, one line per
failure and never the value, then exits 2. In dev the same lines are warnings and start-up
continues.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Final
from urllib.parse import urlsplit

from pydantic import SecretStr

from pycommon.logging import get_logger, log_event
from pycommon.settings import BaseServiceSettings, Env

__all__ = [
    "DEV_DEFAULTS",
    "DEV_KID_PREFIXES",
    "MIN_SECRET_LENGTH",
    "CheckFailure",
    "StartupCheck",
    "check_bank_base_url",
    "check_claim_idle",
    "check_debug_and_docs",
    "check_field_keys",
    "check_jwt_kids",
    "check_mark_rejected_wait",
    "check_republish_after",
    "check_secrets",
    "run_startup_checks",
]

_log = get_logger("pycommon.startup_checks")


@dataclass(frozen=True)
class CheckFailure:
    setting: str
    reason: str


StartupCheck = Callable[[BaseServiceSettings], Iterable[CheckFailure]]

DEV_DEFAULTS: Final = frozenset(
    {"", "changeme", "change-me", "password", "secret", "dev", "test", "REPLACE_ME", "placeholder"}
)
DEV_KID_PREFIXES: Final = ("dev-", "test-")
MIN_SECRET_LENGTH: Final = 16
_DEV_DEFAULTS_LOWER: Final = frozenset(d.lower() for d in DEV_DEFAULTS)


def _is_dev_default(value: str) -> bool:
    return value.strip().lower() in _DEV_DEFAULTS_LOWER


def _is_dev_kid(kid: str) -> bool:
    return kid.lower().startswith(DEV_KID_PREFIXES)


def check_secrets(s: BaseServiceSettings) -> Iterable[CheckFailure]:
    """Every ``SecretStr`` field: present, not a dev default, at least 16 characters."""
    cls = type(s)
    for name, info in cls.model_fields.items():
        annotation = str(info.annotation)
        if "SecretStr" not in annotation:
            continue
        setting = cls.env_name(name)
        value = getattr(s, name)
        if value is None:
            if name == "redis_password" and not s.redis_host:
                continue  # no Redis in this service (bank-stub)
            if (
                name not in BaseServiceSettings.model_fields
                and not info.is_required()
                and info.default is None
            ):
                continue  # optional secret left unset (e.g. SMTP_PASSWORD in uat); services
                # enforce their own "required in prod" rules as extra checks
            yield CheckFailure(setting, "missing")
            continue
        if not isinstance(value, SecretStr):  # pragma: no cover  (pydantic guarantees the type)
            continue
        raw = value.get_secret_value()
        if not raw.strip():
            yield CheckFailure(setting, "missing")
        elif _is_dev_default(raw) or len(raw) < MIN_SECRET_LENGTH:
            yield CheckFailure(setting, "dev_default")


def check_jwt_kids(s: BaseServiceSettings) -> Iterable[CheckFailure]:
    """No ``dev-``/``test-`` kid in ``INTERNAL_JWT_PUBLIC_KEYS`` or in any ``*_KID`` field."""
    cls = type(s)
    keyset = s.internal_jwt_public_keys
    if keyset is not None and any(_is_dev_kid(k) for k in keyset.kids):
        yield CheckFailure(cls.env_name("internal_jwt_public_keys"), "dev_kid")
    for name in cls.model_fields:
        setting = cls.env_name(name)
        if not setting.endswith("_KID"):
            continue
        value = getattr(s, name)
        if isinstance(value, str) and _is_dev_kid(value):
            yield CheckFailure(setting, "dev_kid")


def check_debug_and_docs(s: BaseServiceSettings) -> Iterable[CheckFailure]:
    if s.debug:
        yield CheckFailure("DEBUG", "debug_on")
    if s.docs_enabled:
        yield CheckFailure("DOCS_ENABLED", "docs_on")


def check_field_keys(
    keys_json: str, active_kid: str, *, setting: str = "PAYROLL_FIELD_KEYS"
) -> Iterable[CheckFailure]:
    """A JSON object ``{kid: key}`` holding the active kid, with no dev kid or dev-default key."""
    active_setting = (
        setting.removesuffix("_KEYS") + "_ACTIVE_KID" if setting.endswith("_KEYS") else setting
    )
    try:
        keys = json.loads(keys_json) if keys_json.strip() else None
    except ValueError:
        keys = None
    if not isinstance(keys, dict) or not keys:
        yield CheckFailure(setting, "missing")
        return
    if any(_is_dev_kid(str(k)) for k in keys):
        yield CheckFailure(setting, "dev_kid")
    if any(not isinstance(v, str) or _is_dev_default(v) for v in keys.values()):
        yield CheckFailure(setting, "dev_default")
    if not active_kid or active_kid not in keys:
        yield CheckFailure(active_setting, "missing")
    elif _is_dev_kid(active_kid):
        yield CheckFailure(active_setting, "dev_kid")


_LOCAL_HOSTS: Final = frozenset({"localhost", "localhost.localdomain"})


def _is_local(host: str) -> bool:
    if host in _LOCAL_HOSTS or host.endswith(".localhost"):
        return True
    try:
        addr = ip_address(host)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_unspecified


def check_bank_base_url(url: str, *, env: Env) -> Iterable[CheckFailure]:
    """In prod: not bank-stub, not localhost, not ``*.invalid``, no "placeholder"/"example"."""
    if env is not Env.prod:
        return
    if not url.strip():
        yield CheckFailure("BANK_BASE_URL", "missing")
        return
    host = (urlsplit(url.strip()).hostname or "").lower()
    lowered = url.lower()
    if (
        not host
        or host == "bank-stub"
        or host.startswith("bank-stub.")
        or _is_local(host)
        or host.endswith(".invalid")
        or host == "invalid"
        or "placeholder" in lowered
        or "example" in lowered
    ):
        yield CheckFailure("BANK_BASE_URL", "bank_stub_url")


def _handler_deadline_s(bank_total_deadline_s: float, payroll_call_deadline_s: float) -> float:
    return bank_total_deadline_s + 2 * payroll_call_deadline_s + 10


def check_claim_idle(
    claim_min_idle_ms: int, bank_total_deadline_s: float, payroll_call_deadline_s: float
) -> Iterable[CheckFailure]:
    """``claim_min_idle_ms/1000 > bank_total_deadline_s + 2*payroll_call_deadline_s + 10``."""
    if not claim_min_idle_ms / 1000 > _handler_deadline_s(
        bank_total_deadline_s, payroll_call_deadline_s
    ):
        yield CheckFailure("PAYOUT_CLAIM_MIN_IDLE_MS", "timing_invariant")


def check_republish_after(
    republish_after_s: float,
    max_deliveries: int,
    claim_min_idle_ms: int,
    bank_total_deadline_s: float,
) -> Iterable[CheckFailure]:
    """``republish_after_s > max_deliveries * claim_min_idle_ms/1000 + bank_deadline`` (R1)."""
    if not republish_after_s > max_deliveries * claim_min_idle_ms / 1000 + bank_total_deadline_s:
        yield CheckFailure("PAYOUT_REPUBLISH_AFTER_S", "timing_invariant")


def check_mark_rejected_wait(
    mark_rejected_min_wait_s: float, bank_total_deadline_s: float, payroll_call_deadline_s: float
) -> Iterable[CheckFailure]:
    """``wait > (bank_total_deadline_s + 2*payroll_call_deadline_s + 10) + 30`` (C48)."""
    limit = _handler_deadline_s(bank_total_deadline_s, payroll_call_deadline_s) + 30
    if not mark_rejected_min_wait_s > limit:
        yield CheckFailure("PAYOUT_MARK_REJECTED_MIN_WAIT_S", "timing_invariant")


COMMON_CHECKS: Final[tuple[StartupCheck, ...]] = (
    check_secrets,
    check_jwt_kids,
    check_debug_and_docs,
)


def run_startup_checks(settings: BaseServiceSettings, extra: Sequence[StartupCheck] = ()) -> None:
    """Run the common checks plus ``extra``; in uat/prod a failure exits 2 (C29)."""
    failures: list[CheckFailure] = []
    for check in (*COMMON_CHECKS, *extra):
        failures.extend(check(settings))
    if not failures:
        return
    strict = settings.env is not Env.dev
    level = logging.ERROR if strict else logging.WARNING
    for f in failures:
        log_event(_log, level, "startup.check_failed", setting=f.setting, reason=f.reason)
    if strict:
        raise SystemExit(2)
