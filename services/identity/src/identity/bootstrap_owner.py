"""``python -m identity.bootstrap_owner`` (identity LLD §3.8; PAY-100, PAY-609, R16).

Creates the first user (the owner) with all three roles, on the migrator's ``DB_*`` credentials.
Exit codes: 0 created or skipped (``--if-empty``), 1 unexpected error, 2 invalid arguments or
configuration, 3 refused (users exist), 4 database unreachable or migrations not at head.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final, NoReturn
from uuid import UUID

from alembic.script import ScriptDirectory
from pycommon.db import create_engine, session_factory
from pycommon.errors import db_error_kind
from pycommon.logging import get_logger, log_event, setup_logging
from pycommon.settings import Env, load_settings
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from identity.api.schemas import Email
from identity.db.repositories import AuditRepo, CredentialRepo, InviteRepo, OutboxRepo, UserRepo
from identity.domain import audit_events
from identity.domain.passwords import PasswordHasher, PasswordTooWeakError, check_password_policy
from identity.domain.roles import ALL_ROLES
from identity.paths import MIGRATIONS_DIR
from identity.settings import CoreSettings

__all__ = [
    "EXIT_DB",
    "EXIT_INVALID",
    "EXIT_OK",
    "EXIT_REFUSED",
    "EXIT_UNEXPECTED",
    "BootstrapArgs",
    "main",
    "parse_args",
]

EXIT_OK: Final = 0
EXIT_UNEXPECTED: Final = 1
EXIT_INVALID: Final = 2
EXIT_REFUSED: Final = 3
EXIT_DB: Final = 4

LOCK_SQL: Final = "SELECT pg_advisory_xact_lock(hashtext('identity.bootstrap_owner'))"

_log = get_logger("identity.bootstrap")
_email = TypeAdapter(Email)


class InvalidArgsError(Exception):
    """Exit 2. The message names the problem, never a value."""


class DbNotReadyError(Exception):
    """Exit 4."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse messages can quote the offending argument, so only a fixed text is kept.
        raise InvalidArgsError("usage")


@dataclass(frozen=True)
class BootstrapArgs:
    email: str = field(repr=False)
    company_id: UUID
    name: str = field(repr=False)
    if_empty: bool
    password_env: str | None


def parse_args(argv: Sequence[str]) -> BootstrapArgs:
    """argparse plus the §3.8 value checks; raises ``InvalidArgsError`` (exit 2)."""
    p = _Parser(prog="python -m identity.bootstrap_owner", description=__doc__.splitlines()[0])
    p.add_argument("--email", required=True)
    p.add_argument("--company-id", required=True)
    p.add_argument("--name", default="Owner")
    p.add_argument("--if-empty", action="store_true")
    p.add_argument("--password-env", default=None, metavar="VAR")
    ns = p.parse_args(list(argv))
    try:
        email = str(_email.validate_python(ns.email))
    except ValidationError:
        raise InvalidArgsError("--email is not a valid email address") from None
    try:
        company_id = UUID(ns.company_id)
    except ValueError:
        raise InvalidArgsError("--company-id is not a UUID") from None
    name = str(ns.name).strip()
    if not 1 <= len(name) <= 200:
        raise InvalidArgsError("--name must be 1-200 characters")
    if "\x00" in name:
        raise InvalidArgsError("--name contains a NUL character")
    pw_env = ns.password_env
    if pw_env is not None and not pw_env.strip():
        raise InvalidArgsError("--password-env is empty")
    return BootstrapArgs(email, company_id, name, bool(ns.if_empty), pw_env)


def read_password(args: BootstrapArgs, settings: CoreSettings) -> str | None:
    """The owner password from ``--password-env`` (dev only, policy-checked), or ``None``."""
    if args.password_env is None:
        return None
    if settings.env is not Env.dev:
        raise InvalidArgsError("--password-env is allowed only when ENV=dev")
    password = os.environ.get(args.password_env)
    if not password:
        raise InvalidArgsError("the --password-env variable is not set")
    try:
        check_password_policy(password)
    except PasswordTooWeakError:
        raise InvalidArgsError("the owner password is too weak") from None
    return password


async def _check_ready(engine: AsyncEngine) -> None:
    heads = frozenset(ScriptDirectory(str(MIGRATIONS_DIR)).get_heads())
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(text("SELECT version_num FROM identity.alembic_version"))
            current = frozenset(r[0] for r in rows)
    except Exception as exc:
        raise DbNotReadyError(type(exc).__name__) from exc
    if current != heads:
        raise DbNotReadyError("migrations_not_at_head")


async def bootstrap(
    engine: AsyncEngine, args: BootstrapArgs, password_hash: str | None
) -> tuple[int, UUID | None]:
    """The §3.8 transaction; ``(exit code, owner id)``."""
    await _check_ready(engine)
    sf = session_factory(engine)
    cid = args.company_id
    async with sf() as session, session.begin():
        # pycommon's 1 s lock_timeout is too short to queue behind a concurrent run.
        await session.execute(text("SET LOCAL lock_timeout = '30s'"))
        await session.execute(text(LOCK_SQL))
        users = UserRepo(session)
        if await users.any_exists():
            if args.if_empty:
                log_event(_log, logging.INFO, "identity.bootstrap.skipped", company_id=str(cid))
                return EXIT_OK, None
            log_event(_log, logging.WARNING, "identity.bootstrap.refused", company_id=str(cid))
            return EXIT_REFUSED, None
        active = password_hash is not None
        email_id = None
        owner = await users.insert(
            cid=cid,
            email=args.email,
            name=args.name,
            roles=[str(r) for r in ALL_ROLES],
            status="active" if active else "pending",
            created_by=None,
            activated=active,
        )
        if password_hash is not None:
            await CredentialRepo(session).insert(
                user_id=owner.id, cid=cid, password_hash=password_hash
            )
            mode = "password"
        else:
            invite_id = await InviteRepo(session).insert(cid=cid, user_id=owner.id, created_by=None)
            email_id = await OutboxRepo(session).queue(
                cid=cid, template="invite", recipient=owner.email, invite_id=invite_id
            )
            mode = "invite"
        await AuditRepo(session).append(
            cid=cid,
            event=audit_events.USER_BOOTSTRAPPED,
            target_user_id=owner.id,
            details={"mode": mode},
        )
    log_event(
        _log,
        logging.INFO,
        "identity.bootstrap.completed",
        user_id=str(owner.id),
        company_id=str(cid),
        mode=mode,
    )
    if email_id is not None:
        log_event(
            _log,
            logging.INFO,
            "email.queued",
            email_id=str(email_id),
            template="invite",
            user_id=str(owner.id),
        )
    return EXIT_OK, owner.id


def _failed(reason: str, code: int, **extra: Any) -> int:
    log_event(_log, logging.ERROR, "identity.bootstrap.failed", reason=reason, **extra)
    return code


async def _run(settings: CoreSettings, args: BootstrapArgs, password: str | None) -> int:
    password_hash = None
    if password is not None:
        hasher = PasswordHasher(
            time_cost=settings.identity_argon2_time_cost,
            memory_kib=settings.identity_argon2_memory_kib,
            parallelism=settings.identity_argon2_parallelism,
            max_concurrency=1,
        )
        password_hash = await hasher.hash(password)
    engine = create_engine(settings)
    try:
        code, _ = await bootstrap(engine, args, password_hash)
        return code
    except DbNotReadyError:
        return _failed("db_unavailable", EXIT_DB)
    except Exception as exc:
        if db_error_kind(exc) is not None:
            return _failed("db_unavailable", EXIT_DB)
        raise
    finally:
        await engine.dispose()


def main(argv: Sequence[str]) -> int:
    try:
        settings = load_settings(CoreSettings)
    except SystemExit:
        return EXIT_INVALID  # load_settings logged settings.invalid (field names only)
    setup_logging(settings)
    try:
        args = parse_args(argv)
        if args.company_id != settings.company_id:
            raise InvalidArgsError("--company-id differs from COMPANY_ID")
        password = read_password(args, settings)
    except InvalidArgsError as exc:
        return _failed("invalid_args", EXIT_INVALID, detail=str(exc))
    try:
        return asyncio.run(_run(settings, args, password))
    except Exception as exc:
        log_event(
            _log,
            logging.ERROR,
            "identity.bootstrap.failed",
            reason="unexpected",
            error_type=type(exc).__name__,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
