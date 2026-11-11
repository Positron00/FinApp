"""Graceful shutdown (design §3.3.7; PAY-408, PAY-403, PAY-NFR-02).

In process: the app's lifespan shutdown, which is what uvicorn runs on SIGTERM, with 16
handlers held inside the bank call. In a real process: ``python -m pycommon.serve`` and SIGTERM.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import redis.asyncio as aioredis
from fastapi import FastAPI
from pycommon.db import new_id
from pycommon.testing import JsonLogCapture, PgBootstrapped, RedisAcl

from tests.helpers import API_KEY, COMPANY_ID, TOKENS, envelope_dict
from tests.integration.conftest import GROUP, STREAM, Stack, runtime, wait_for

N = 16  # PAYOUT_CONCURRENCY's default: every slot holds a message
GRACE_S = 75  # terminationGracePeriodSeconds (plan §2.2)
DEPLOYED_OTLP_TIMEOUT_S = 3  # OTEL_EXPORTER_OTLP_TIMEOUT in Compose and k8s (C67)


class GatedBank(httpx.AsyncBaseTransport):
    """bank-stub behind a gate: every POST /v1/transfers waits for ``gate`` before it's sent."""

    def __init__(self, inner: httpx.AsyncBaseTransport, expected: int) -> None:
        self.inner = inner
        self.gate = asyncio.Event()
        self.arrived = 0
        self.all_arrived = asyncio.Event()
        self.expected = expected

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            self.arrived += 1
            if self.arrived >= self.expected:
                self.all_arrived.set()
            await self.gate.wait()
        return await self.inner.handle_async_request(request)


async def _lag(r: aioredis.Redis) -> int:
    groups: Any = await r.xinfo_groups(STREAM)
    [g] = [g for g in groups if g["name"] in {GROUP, GROUP.encode()}]
    return int(g["lag"])


@contextlib.asynccontextmanager
async def _started(app: FastAPI) -> AsyncIterator[Any]:
    life = app.router.lifespan_context(app)
    await life.__aenter__()
    try:
        yield life
    finally:
        with contextlib.suppress(Exception):
            await life.__aexit__(None, None, None)


async def _sixteen_in_flight(stack: Stack, **over: Any) -> tuple[FastAPI, GatedBank, str]:
    bank = GatedBank(httpx.ASGITransport(app=stack.bank_app, raise_app_exceptions=False), N)
    app = stack.build_worker(bank_transport=bank, payout_consumer_name="draining", **over)
    return app, bank, (await stack.approved_run([f"0001234567{i:02d}" for i in range(N)]))["id"]


async def test_shutdown_stops_reading_and_drains_in_flight(
    stack: Stack, caplog_json: JsonLogCapture
) -> None:
    """PAY-408, PAY-403: with 16 handlers held in the bank call, shutdown stops the reader and
    the claim loop at once (an entry added afterwards is never delivered), /readyz turns 503,
    and the 16 handlers finish within PAYOUT_SHUTDOWN_DRAIN_S: 16 transfers, all paid and
    ACKed; worker.shutdown.completed reports drained 16, cancelled 0."""
    app, bank, run_id = await _sixteen_in_flight(stack, payout_shutdown_drain_s=30)
    async with _started(app) as life:
        consumer = runtime(app).consumer
        await asyncio.wait_for(bank.all_arrived.wait(), 30)
        assert consumer.in_flight == N
        stopper = asyncio.create_task(life.__aexit__(None, None, None))
        try:
            await wait_for(lambda: _true(consumer.stopping))
            started = caplog_json.events("worker.shutdown.started")
            assert [e["in_flight"] for e in started] == [N]
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://w"
            ) as client:
                ready = await client.get("/readyz")
            assert ready.status_code == 503
            late = await stack.redis.xadd(STREAM, {"event_id": "late", "type": "x"})
            bank.gate.set()
            await asyncio.wait_for(asyncio.shield(stopper), 30)
        finally:
            bank.gate.set()
            with contextlib.suppress(Exception):
                await stopper
    [done] = caplog_json.events("worker.shutdown.completed")
    assert (done["drained"], done["cancelled"]) == (N, 0)
    assert done["duration_s"] < 30
    assert await stack.transfers() == N
    assert {p["status"] for p in await stack.payouts(run_id)} == {"paid"}
    assert await stack.pending() == 0
    assert await _lag(stack.redis) == 1  # the late entry was never read
    pending: Any = await stack.redis.xpending_range(STREAM, GROUP, min=late, max=late, count=1)
    assert pending == []


async def test_drain_timeout_cancels_and_leaves_entries_pending(
    stack: Stack, caplog_json: JsonLogCapture
) -> None:
    """PAY-408, PAY-NFR-02: handlers still running when PAYOUT_SHUTDOWN_DRAIN_S (1 s here) runs
    out are cancelled; their entries stay pending (no XACK, no DLQ) and shutdown ends within the
    drain plus pycommon's 5 s margin. Another consumer then reclaims them: one transfer each."""
    app, bank, run_id = await _sixteen_in_flight(stack, payout_shutdown_drain_s=1)
    loop = asyncio.get_running_loop()
    async with _started(app) as life:
        await asyncio.wait_for(bank.all_arrived.wait(), 30)
        t0 = loop.time()
        await life.__aexit__(None, None, None)
        elapsed = loop.time() - t0
    assert elapsed < 1 + 5
    [done] = caplog_json.events("worker.shutdown.completed")
    assert (done["drained"], done["cancelled"]) == (0, N)
    assert await stack.pending() == N
    assert await stack.transfers() == 0

    await stack.worker(payout_consumer_name="survivor")

    async def all_paid() -> bool:
        return {p["status"] for p in await stack.payouts(run_id)} == {"paid"}

    await wait_for(all_paid, within=30)
    assert await stack.transfers() == N

    async def acked() -> bool:
        return await stack.pending() == 0

    await wait_for(acked)


async def _true(value: bool) -> bool:
    return value


# --- a real process and SIGTERM -----------------------------------------------------------------


class _Fakes:
    """payroll's internal endpoints and the bank on one stdlib HTTP server (the subprocess can't
    reach the in-process apps). The bank holds each POST until ``release`` is set."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.bank_posts = threading.Event()
        self.results: list[dict[str, Any]] = []
        self.instructions: dict[str, dict[str, Any]] = {}

    def handler(self) -> type[BaseHTTPRequestHandler]:
        fakes = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return

            def _send(self, status: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                payout_id = self.path.split("/")[4]
                self._send(200, fakes.instructions[payout_id])

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/v1/transfers":
                    fakes.bank_posts.set()
                    fakes.release.wait(30)
                    self._send(
                        201,
                        {
                            "transfer_id": str(new_id()),
                            "status": "settled",
                            "reason": None,
                            "bank_reference": "BSR-0123456789ABCDEF",
                        },
                    )
                    return
                payout_id = self.path.split("/")[4]
                fakes.results.append(body)
                self._send(
                    200, {"payout_id": payout_id, "attempt": body["attempt"], "outcome": "settled"}
                )

        return H


@pytest.fixture
def fakes() -> Iterator[tuple[_Fakes, int]]:
    f = _Fakes()
    server = ThreadingHTTPServer(("127.0.0.1", 0), f.handler())
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f, server.server_address[1]
    finally:
        f.release.set()
        server.shutdown()
        server.server_close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def _worker_env(
    pg: PgBootstrapped, r: RedisAcl, fake_port: int, port: int, otlp_timeout_s: int
) -> dict[str, str]:
    d, rr = pg.env_for("payouts_app"), r.env_for("payout-worker")
    keep = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "VIRTUAL_ENV", "TMPDIR"}}
    return {
        **keep,
        **d,
        **rr,
        "ENV": "dev",
        "DB_NAME": "payrun",
        "DB_SCHEMA": "payouts",
        "COMPANY_ID": str(COMPANY_ID),
        "PYCOMMON_APP": "payout_worker.main:app",
        "PORT": str(port),
        "PAYOUT_CONSUMER_NAME": "sigterm",
        "PAYOUT_SHUTDOWN_DRAIN_S": "2",
        "PAYOUT_XREAD_BLOCK_MS": "200",
        "PAYOUT_CLAIM_INTERVAL_S": "0.2",
        "PAYOUT_PAYROLL_BASE_URL": f"http://127.0.0.1:{fake_port}",
        "BANK_BASE_URL": f"http://127.0.0.1:{fake_port}",
        "BANK_STUB_API_KEY": API_KEY,
        "PAYOUT_WORKER_JWT_PRIVATE_KEY": TOKENS.private_pems["payout-worker"],
        "PAYOUT_WORKER_JWT_KID": TOKENS.kids["payout-worker"],
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9",  # nothing listens: drop quietly
        "OTEL_EXPORTER_OTLP_TIMEOUT": str(otlp_timeout_s),
        "OTEL_METRIC_EXPORT_INTERVAL": "600000",
    }


async def _ready(port: int) -> bool:
    try:
        async with httpx.AsyncClient() as c:
            return (await c.get(f"http://127.0.0.1:{port}/readyz", timeout=1)).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.mark.parametrize(
    ("released", "otlp_timeout_s"),
    [(True, 1), (False, 1), (False, DEPLOYED_OTLP_TIMEOUT_S)],
    ids=["drained", "drain_timeout", "drain_timeout_deployed_otlp_timeout"],
)
async def test_sigterm_in_a_real_process(
    stack: Stack,
    fakes: tuple[_Fakes, int],
    tmp_path: Path,
    released: bool,
    otlp_timeout_s: int,
) -> None:
    """PAY-408: a real `python -m pycommon.serve` worker gets SIGTERM while one handler is held
    in the bank call. It stops reading (an entry added after the signal is never delivered),
    then either finishes the held message (released within PAYOUT_SHUTDOWN_DRAIN_S: result
    posted, ACKed) or cancels it at the drain (left pending for another consumer). It exits
    (status 0, or SIGTERM re-raised by uvicorn after the graceful shutdown) well within the 75 s
    grace period: drain 2 s + ≤ 10 s for close and flush. The OTLP endpoint is unreachable, so
    the flush waits out OTEL_EXPORTER_OTLP_TIMEOUT per exporter; the third case uses the value
    Compose and Kubernetes deploy (3 s, C67): three exporters flushed in turn still fit in the
    10 s, so a 60 s drain + flush stays under the 75 s grace."""
    fake, fake_port = fakes
    port = _free_port()
    env = envelope_dict(event_id=new_id(), payout_id=str(new_id()))
    data = env["data"]
    fake.instructions[data["payout_id"]] = {
        "payout_id": data["payout_id"],
        "attempt": 1,
        "amount_cents": data["amount_cents"],
        "currency": "USD",
        "routing": "021000021",
        "account": "000123456789",
        "requested_at": _now_iso(),
    }
    log_path = tmp_path / "worker.log"
    with log_path.open("wb") as log:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pycommon.serve",
            env=_worker_env(stack.pg, stack.redis_acl, fake_port, port, otlp_timeout_s),
            cwd=tmp_path,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            await wait_for(lambda: _ready(port), within=60, interval=0.2)
            entry: Any = await stack.redis.xadd(
                STREAM,
                {
                    "event_id": env["event_id"],
                    "type": "payout.requested",
                    "envelope": json.dumps(env),
                },
            )
            await wait_for(lambda: _true(fake.bank_posts.is_set()), within=30)
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            proc.send_signal(signal.SIGTERM)
            await wait_for(lambda: _logged(log_path, "worker.shutdown.started"), within=20)
            late: Any = await stack.redis.xadd(STREAM, {"event_id": "late", "type": "x"})
            if released:
                fake.release.set()
            await asyncio.wait_for(proc.wait(), GRACE_S)
            elapsed = loop.time() - t0
        finally:
            fake.release.set()
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
    lines = [json.loads(x) for x in log_path.read_text().splitlines() if x.startswith("{")]
    events = {line.get("event"): line for line in lines}
    # uvicorn re-raises the captured SIGTERM once its graceful shutdown is complete, so the
    # status is "terminated by SIGTERM" (143 in a container), which Kubernetes treats as normal.
    assert proc.returncode in {0, -signal.SIGTERM}, log_path.read_text()[-2000:]
    assert "Application shutdown complete." in log_path.read_text()
    assert elapsed < 2 + 10 < GRACE_S
    done = events["worker.shutdown.completed"]
    assert (done["drained"], done["cancelled"]) == ((1, 0) if released else (0, 1))
    assert "service.stopped" in events
    rows: Any = await stack.redis.xpending_range(STREAM, GROUP, min="-", max="+", count=10)
    still = {(row["message_id"].decode()) for row in rows}
    if released:
        assert [r["outcome"] for r in fake.results] == ["settled"]
        assert still == set()
    else:
        assert fake.results == []
        assert still == {entry.decode()}
    assert late.decode() not in still
    assert await _lag(stack.redis) == 1  # the late entry was never read


def _now_iso() -> str:
    from datetime import UTC, datetime  # noqa: PLC0415

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


async def _logged(path: Path, event: str) -> bool:
    return f'"event": "{event}"' in await asyncio.to_thread(path.read_text)
