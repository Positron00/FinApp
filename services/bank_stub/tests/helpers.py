"""Helpers shared by the bank-stub suites."""

from __future__ import annotations

import contextlib
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx
from bank_stub.db.repo import NewTransfer, TransferRow
from bank_stub.main import build_app
from bank_stub.runtime import Runtime
from bank_stub.settings import Settings
from fastapi import FastAPI
from pydantic import SecretStr

COMPANY_ID = UUID("0193a000-0000-7000-8000-000000000001")
# Built at runtime so no literal looks like a credential (testing.md §9).
API_KEY = secrets.token_hex(20)
STRONG_SECRET = "s3cr3t-Value-For-Tests-0123456789"
ROUTING = "021000021"
ACCOUNT = "000123456789"
CLOSED_ACCOUNT = "000123450000"
AMOUNT = 412_345
KEY = "0193f1d2-6c4a-7000-8000-000000000001:1"


def make_settings(**overrides: Any) -> Settings:
    """Settings built without reading env or ``.env`` (``model_validate`` skips the sources)."""
    values: dict[str, Any] = {
        "company_id": COMPANY_ID,
        "db_password": SecretStr(STRONG_SECRET),
        "bank_stub_api_key": SecretStr(API_KEY),
        "bank_stub_failure_rate": 0.0,
        "bank_stub_latency_ms": 0,
    }
    values.update(overrides)
    return Settings.model_validate(values)


def body(account: str = ACCOUNT, amount: int = AMOUNT, routing: str = ROUTING) -> dict[str, Any]:
    return {"amount_cents": amount, "currency": "USD", "routing": routing, "account": account}


def headers(key: str | None = KEY, api_key: str | None = API_KEY) -> dict[str, str]:
    h: dict[str, str] = {}
    if key is not None:
        h["Idempotency-Key"] = key
    if api_key is not None:
        h["X-Api-Key"] = api_key
    return h


@dataclass
class FakeRepo:
    """In-memory ``TransferRepoPort`` for unit tests."""

    rows: dict[str, TransferRow] = field(default_factory=dict)
    inserted: list[NewTransfer] = field(default_factory=list)
    lose_race: bool = False

    async def get_by_key(self, key: str) -> TransferRow | None:
        return self.rows.get(key)

    async def insert_or_get(self, new: NewTransfer) -> tuple[TransferRow, bool]:
        existing = self.rows.get(new.idempotency_key)
        if existing is not None:
            return existing, False
        row = TransferRow(
            id=new.id,
            idempotency_key=new.idempotency_key,
            status=new.status,
            reason=new.reason,
            bank_reference=new.bank_reference,
        )
        self.rows[new.idempotency_key] = row
        self.inserted.append(new)
        if self.lose_race:  # a concurrent request inserted first: report "not created"
            return row, False
        return row, True


@dataclass
class Sleeps:
    calls: list[float] = field(default_factory=list)

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@contextlib.asynccontextmanager
async def running(
    settings: Settings, *, instrument: bool = False
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    app = build_app(settings, instrument=instrument)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://bank-stub") as client:
            yield app, client


def runtime(app: FastAPI) -> Runtime:
    rt: Runtime = app.state.bank_stub
    return rt
