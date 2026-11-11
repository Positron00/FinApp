"""A slow *link* to Postgres is bounded too (S8 slow-DB finding; PAY-605; HLD §4 "Slow DB").

``statement_timeout`` only bounds execution on the server. With latency on the network every round
trip (pre-ping, prepare, execute, commit) is slow but no single statement is, so before the fix a
request on a warm pooled connection took ~10 round trips (≈ 20 s at 2 s) and then succeeded. A
tiny in-process TCP proxy adds latency to server → client bytes, like Toxiproxy's latency toxic.
"""

import asyncio
import contextlib
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient
from pycommon.app import create_app, get_ctx
from pycommon.db import map_db_errors, transaction
from sqlalchemy import text

from tests.integration.conftest import Pg, free_port

pytestmark = pytest.mark.integration

BUDGET_S = 4.0  # DB_STATEMENT_TIMEOUT_MS 2000 / 1000 + DB_POOL_TIMEOUT_S 2


@dataclass
class SlowProxy:
    port: int
    delay_s: float = 0.0


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, proxy: SlowProxy,
                *, slow: bool) -> None:  # fmt: skip
    with contextlib.suppress(ConnectionError, asyncio.CancelledError):
        while data := await reader.read(65536):
            if slow and proxy.delay_s:
                await asyncio.sleep(proxy.delay_s)
            writer.write(data)
            await writer.drain()
    writer.close()


@pytest.fixture
def slow_proxy(pg: Pg) -> Iterator[SlowProxy]:
    proxy = SlowProxy(port=free_port())
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    async def handle(cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        sr, sw = await asyncio.open_connection(pg.host, pg.port)
        await asyncio.gather(_pipe(cr, sw, proxy, slow=False), _pipe(sr, cw, proxy, slow=True))

    stop = asyncio.Event()

    async def serve() -> None:
        server = await asyncio.start_server(handle, "127.0.0.1", proxy.port)
        ready.set()
        async with server:
            await stop.wait()

    t = threading.Thread(target=lambda: loop.run_until_complete(serve()), daemon=True)
    t.start()
    ready.wait(5)
    try:
        yield proxy
    finally:
        loop.call_soon_threadsafe(stop.set)
        t.join(5)


def _client(pg: Pg, proxy: SlowProxy) -> TestClient:
    r = APIRouter()

    @r.get("/tx")
    async def tx(request: Request) -> dict[str, int]:
        sf = get_ctx(request).sessions
        assert sf is not None
        async with transaction(sf) as s:
            await s.execute(text("SELECT 1"))
            v = (await s.execute(text("SELECT 2"))).scalar_one()
        return {"v": v}

    @r.get("/mapped")
    async def mapped(request: Request) -> dict[str, int]:
        sf = get_ctx(request).sessions
        assert sf is not None
        with map_db_errors("mapped"):
            async with sf() as s, s.begin():
                await s.execute(text("SELECT 1"))
                return {"v": (await s.execute(text("SELECT 3"))).scalar_one()}

    settings = pg.settings(db_host="127.0.0.1", db_port=proxy.port)
    app = create_app(settings, public_routers=[r], use_redis=False, use_jwt=False,
                     instrument=False)  # fmt: skip
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("path", ["/tx", "/mapped"])
def test_slow_link_on_a_warm_pool_is_db_timeout_within_budget(
    pg: Pg, slow_proxy: SlowProxy, path: str
) -> None:
    """2 s of latency per server reply: 503 db_timeout at the 4 s budget (cleanup included), never
    a 20 s success; later requests fail at the 2 s connect timeout; recovery once it's gone."""
    with _client(pg, slow_proxy) as c:
        assert c.get(path).status_code == 200  # the pool now holds a warm connection
        slow_proxy.delay_s = 2.0
        for _ in range(3):
            start = time.monotonic()
            resp = c.get(path)
            elapsed = time.monotonic() - start
            assert resp.status_code == 503, resp.text
            assert resp.json()["error"]["code"] == "db_timeout"
            assert elapsed < BUDGET_S + 1.0, f"{elapsed:.1f}s"
        slow_proxy.delay_s = 0.0
        deadline = time.monotonic() + 15
        while c.get(path).status_code != 200:
            assert time.monotonic() < deadline, "did not recover"
            time.sleep(0.2)
