"""Integration fixtures (identity LLD §7.2).

Postgres 16 with the bootstrap SQL and Redis 7 with the ACL file come from ``pycommon.testing``;
migrations run as ``identity_migrator`` and the app connects as ``identity_app`` (Redis user
``identity``), so every test exercises the real grants. SMTP is an in-process aiosmtpd server.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from email import message_from_bytes
from email.message import EmailMessage, Message
from typing import Any, Final
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
import httpx
import pytest
import redis.asyncio as aioredis
from aiosmtpd.controller import Controller
from fastapi import FastAPI
from identity.db.repositories import CredentialRepo, UserRepo
from identity.domain.roles import normalise_roles
from identity.email.smtp import EmailSender
from identity.main import build_app
from identity.runtime import IdentityRuntime
from identity.settings import IdentitySettings
from pycommon.db import transaction
from pycommon.testing import PgBootstrapped, RedisAcl, TokenFactory
from pydantic import SecretStr

from tests.helpers import COMPANY_ID, make_settings, web_only_keyset

TABLES: Final = "users, credentials, invites, password_resets, audit_log, email_outbox"
TOKEN_RE: Final = re.compile(r"#token=([A-Za-z0-9_-]{43})")
CLIENT_IP: Final = "203.0.113.7"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# --- SMTP --------------------------------------------------------------------------------------


class _Collect:
    def __init__(self, sink: list[Message]) -> None:
        self.sink = sink

    async def handle_DATA(self, server: Any, session: Any, envelope: Any) -> str:  # noqa: N802
        self.sink.append(message_from_bytes(envelope.content))
        return "250 OK"


@dataclass
class FakeSmtp:
    """An in-process SMTP server (aiosmtpd) that can be stopped and restarted on its port."""

    port: int
    messages: list[Message] = field(default_factory=list)
    _controller: Any = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def start(self) -> None:
        with self._lock:
            if self._controller is None:
                c = Controller(_Collect(self.messages), hostname="127.0.0.1", port=self.port)
                c.start()
                self._controller = c

    def stop(self) -> None:
        with self._lock:
            if self._controller is not None:
                self._controller.stop()
                self._controller = None


def text_of(msg: Message) -> str:
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True)
            assert isinstance(payload, bytes)
            return payload.decode()
    raise AssertionError("no text/plain part")


def token_in(msg: Message) -> str:
    m = TOKEN_RE.search(text_of(msg))
    assert m is not None, "no token link in the email"
    return m.group(1)


@pytest.fixture(scope="session")
def smtp_server() -> Iterator[FakeSmtp]:
    # The fake server's own protocol log (logger "mail.log") isn't identity's telemetry.
    logging.getLogger("mail.log").disabled = True
    server = FakeSmtp(port=free_port())
    server.start()
    try:
        yield server
    finally:
        server.stop()


class CapturingSender:
    """An ``EmailSender`` that keeps the messages in memory."""

    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []

    def available(self) -> bool:
        return True

    async def send(self, msg: EmailMessage) -> None:
        self.messages.append(msg)


# --- database and Redis ------------------------------------------------------------------------


@pytest.fixture(scope="session")
def migrated(
    pg_bootstrapped: PgBootstrapped, alembic_upgrade: Callable[..., None]
) -> PgBootstrapped:
    alembic_upgrade("identity")
    return pg_bootstrapped


async def connect(pg: PgBootstrapped, role: str) -> Any:
    e = pg.env_for(role)
    return await asyncpg.connect(
        host=e["DB_HOST"],
        port=int(e["DB_PORT"]),
        user=e["DB_USER"],
        password=e["DB_PASSWORD"],
        database="payrun",
    )


@pytest.fixture
async def admin_db(migrated: PgBootstrapped) -> AsyncIterator[Any]:
    """A superuser connection for assertions."""
    conn = await connect(migrated, "postgres")
    try:
        yield conn
    finally:
        await conn.close()


def redis_admin(r: RedisAcl) -> aioredis.Redis:
    return aioredis.Redis(
        host=r.host, port=r.port, password=r.passwords["REDIS_ADMIN_PASSWORD"], socket_timeout=5
    )


@pytest.fixture(autouse=True)
async def clean(migrated: PgBootstrapped, redis_acl: RedisAcl) -> None:
    """Each test starts with empty identity tables and an empty Redis."""
    conn = await connect(migrated, "identity_migrator")
    try:
        await conn.execute(f"TRUNCATE {TABLES} CASCADE")
    finally:
        await conn.close()
    r = redis_admin(redis_acl)
    try:
        await r.flushdb()
    finally:
        await r.aclose()


@pytest.fixture
async def redis_admin_client(redis_acl: RedisAcl) -> AsyncIterator[aioredis.Redis]:
    r = redis_admin(redis_acl)
    try:
        yield r
    finally:
        await r.aclose()


# --- settings, app and tokens ------------------------------------------------------------------


@pytest.fixture
def settings_for(
    migrated: PgBootstrapped,
    redis_acl: RedisAcl,
    token_factory: TokenFactory,
    smtp_server: FakeSmtp,
) -> Callable[..., IdentitySettings]:
    db = migrated.env_for("identity_app")
    rd = redis_acl.env_for("identity")

    def make(**overrides: Any) -> IdentitySettings:
        values: dict[str, Any] = {
            "db_host": db["DB_HOST"],
            "db_port": int(db["DB_PORT"]),
            "db_user": db["DB_USER"],
            "db_password": SecretStr(db["DB_PASSWORD"]),
            "redis_host": rd["REDIS_HOST"],
            "redis_port": int(rd["REDIS_PORT"]),
            "redis_username": rd["REDIS_USERNAME"],
            "redis_password": SecretStr(rd["REDIS_PASSWORD"]),
            "internal_jwt_public_keys": web_only_keyset(token_factory),
            "smtp_host": "127.0.0.1",
            "smtp_port": smtp_server.port,
            "identity_smtp_timeout_s": 2.0,
        }
        values.update(overrides)
        return make_settings(**values)

    return make


@dataclass
class Harness:
    app: FastAPI
    client: httpx.AsyncClient
    tokens: TokenFactory
    sender: CapturingSender | None

    @property
    def rt(self) -> IdentityRuntime:
        rt = self.app.state.identity
        assert isinstance(rt, IdentityRuntime)
        return rt

    def web_headers(self, ip: str | None = CLIENT_IP) -> dict[str, str]:
        token = self.tokens.mint(aud="identity", sub="service:web", cid=COMPANY_ID)
        headers = {"Authorization": f"Bearer {token}"}
        if ip is not None:
            headers["X-Client-IP"] = ip
        return headers

    def user_headers(self, user_id: UUID, roles: tuple[str, ...] = ()) -> dict[str, str]:
        token = self.tokens.mint(
            aud="identity", sub=str(user_id), cid=COMPANY_ID, roles=roles, name="X", sid="s"
        )
        return {"Authorization": f"Bearer {token}"}

    async def login(
        self, email: str, password: str, *, ip: str | None = CLIENT_IP
    ) -> httpx.Response:
        return await self.client.post(
            "/v1/auth/login",
            json={"email": email, "password": password},
            headers=self.web_headers(ip),
        )

    async def accept(self, token: str, password: str) -> httpx.Response:
        return await self.client.post(
            "/v1/invites/accept",
            json={"token": token, "password": password},
            headers=self.web_headers(),
        )

    async def create_user(
        self,
        email: str,
        *,
        password: str | None = None,
        roles: tuple[str, ...] = ("admin",),
        status: str = "active",
        name: str = "Test User",
    ) -> UUID:
        rt = self.rt
        pw_hash = await rt.hasher.hash(password) if password is not None else None
        async with transaction(rt.sessions) as session:
            user = await UserRepo(session).insert(
                cid=COMPANY_ID,
                email=email,
                name=name,
                roles=normalise_roles(roles),
                status=status,
                created_by=None,
                activated=status == "active",
            )
            if pw_hash is not None:
                await CredentialRepo(session).insert(
                    user_id=user.id, cid=COMPANY_ID, password_hash=pw_hash
                )
        return user.id


@asynccontextmanager
async def run_app(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://identity") as c:
            yield c


HarnessFactory = Callable[..., AbstractAsyncContextManager[Harness]]


@pytest.fixture
def harness(
    settings_for: Callable[..., IdentitySettings], token_factory: TokenFactory
) -> HarnessFactory:
    """``async with harness(**settings_overrides) as h``: a started app and a client.

    ``sender="capture"`` (the default) keeps emails in memory; ``sender="smtp"`` uses the real
    SmtpSender against the aiosmtpd server. The relay task runs only with ``run_relay=True``.
    """

    @asynccontextmanager
    async def make(
        *,
        sender: str = "capture",
        run_relay: bool = False,
        instrument: bool = False,
        clock: Any = None,
        **overrides: Any,
    ) -> AsyncIterator[Harness]:
        s = settings_for(**overrides)
        capture: CapturingSender | None = CapturingSender() if sender == "capture" else None
        email_sender: EmailSender | None = capture
        app = build_app(
            s, instrument=instrument, clock=clock, sender=email_sender, run_relay=run_relay
        )
        async with run_app(app) as client:
            yield Harness(app, client, token_factory, capture)

    return make


async def wait_until(pred: Callable[[], Any], timeout_s: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        result = pred()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)
