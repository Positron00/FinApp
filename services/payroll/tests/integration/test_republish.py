"""Re-publish non-final attempts (design §5.3; PAY-407, PAY-409, PAY-NFR-03, R1, R34; plan §12.1
"Redis data loss", "Re-publish racing a live consumer", "Poison message looping through
re-publish"). Time is a ``FakeClock`` on the runtime: no sleeps."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest
from payroll.events.relay import build_relay
from pycommon.streams import StreamProducer
from pycommon.testing import FakeClock, JsonLogCapture, OtelMemory

from tests.integration.conftest import Api, ApiFactory

pytestmark = pytest.mark.integration


async def _setup(api: Api, db: Any, n: int = 1) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """An approved run whose first publications were relayed; the clock starts at the latest
    ``last_published_at``."""
    run = await api.approved_run(n)
    await api.mark_outbox_sent(db)
    await _clock_at_last_publish(api, db)
    return run, await api.payouts(run["id"])


async def _tick(api: Api) -> None:
    assert api.rt.maintenance is not None
    assert await api.rt.maintenance.tick()


def _advance(api: Api, seconds: float) -> None:
    clock = api.rt.clock
    assert isinstance(clock, FakeClock)
    clock.advance(seconds)


async def _attempt(db: Any, pid: str, attempt: int = 1) -> dict[str, Any]:
    return dict(
        await db.fetchrow(
            "SELECT * FROM payroll.payout_attempts WHERE payout_id = $1 AND attempt = $2",
            pid,
            attempt,
        )
    )


async def _unsent(db: Any) -> list[dict[str, Any]]:
    rows = await db.fetch(
        "SELECT id, payout_id, attempt, payload FROM payroll.outbox WHERE sent_at IS NULL"
    )
    return [
        {**dict(r), "payout_id": str(r["payout_id"]), "payload": json.loads(r["payload"])}
        for r in rows
    ]


async def _clock_at_last_publish(api: Api, db: Any) -> None:
    start: datetime = await db.fetchval(
        "SELECT max(last_published_at) FROM payroll.payout_attempts"
    )
    api.rt.clock = FakeClock(start=start)


async def test_not_due_then_due(api: Api, db: Any, caplog_json: JsonLogCapture) -> None:
    """PAY-407, R34: at 360 s nothing happens; just past it a new outbox row with a fresh
    event_id and republish_seq = publish_count before the increment (1), publish_count 2 and
    last_published_at = now; logged payout.republished."""
    _, [p] = await _setup(api, db)
    _advance(api, 360)
    await _tick(api)
    assert await _unsent(db) == []
    _advance(api, 1)
    await _tick(api)
    [row] = await _unsent(db)
    assert (row["payout_id"], row["attempt"]) == (p["id"], 1)
    assert row["payload"]["data"]["republish_seq"] == 1
    first_event = await db.fetchval(
        "SELECT id FROM payroll.outbox WHERE payout_id = $1 ORDER BY created_at LIMIT 1", p["id"]
    )
    assert row["id"] != first_event
    a = await _attempt(db, p["id"])
    assert (a["publish_count"], a["last_published_at"]) == (2, api.rt.clock.now())
    [log] = caplog_json.events("payout.republished")
    assert (log["republish_seq"], log["event_id"]) == (1, str(row["id"]))


async def test_backoff_sequence_and_limit(
    api_factory: ApiFactory, db: Any, otel_memory: OtelMemory
) -> None:
    """PAY-407, R1, PAY-409: waits 360, 720, 900, 900… s; after 10 re-publishes the attempt
    becomes needs_operator (republish_limit, needs_operator_at, audit, metric) and re-publishing
    stops."""
    api = await api_factory(instrument=True)
    _, [p] = await _setup(api, db)
    expected_waits = [360, 720, *([900] * 8)]
    for n, wait in enumerate(expected_waits, start=1):
        _advance(api, wait)
        await _tick(api)
        assert await _unsent(db) == [], f"re-publish {n} came early"
        _advance(api, 1)
        await _tick(api)
        [row] = await _unsent(db)
        assert row["payload"]["data"]["republish_seq"] == n
        await api.mark_outbox_sent(db)
    assert (await _attempt(db, p["id"]))["publish_count"] == 11
    _advance(api, 901)
    await _tick(api)
    a = await _attempt(db, p["id"])
    assert (a["status"], a["failure_reason"]) == ("needs_operator", "republish_limit")
    assert a["needs_operator_at"] == api.rt.clock.now()
    assert await _unsent(db) == []
    audit = await db.fetchrow(
        "SELECT actor_id, details::text AS d FROM payroll.audit_events "
        "WHERE event = 'payout.needs_operator'"
    )
    assert audit["actor_id"] == "system:maintenance"
    assert json.loads(audit["d"]) == {"attempt": 1, "reason": "republish_limit"}
    [(_, republished)] = otel_memory.metric_points("payroll_payouts_republished")
    assert republished == 10
    outcomes = {a.get("outcome"): v for a, v in otel_memory.metric_points("payroll_payouts")}
    assert outcomes.get("needs_operator") == 1
    _advance(api, 10_000)
    await _tick(api)
    assert await _unsent(db) == []  # a needs_operator attempt is never re-published
    payout = (await api.payouts(p["pay_run_id"]))[0]
    assert payout["status"] == "requested"
    summary = (
        await api.client.get(f"/v1/pay-runs/{p['pay_run_id']}", headers=api.approver())
    ).json()
    assert (summary["status"], summary["counts"]["blocked"]) == ("processing", 1)


async def test_redrive_restarts_the_backoff(api: Api, db: Any) -> None:
    """PAY-407, PAY-502, C13: after a redrive the next re-publish is due 360 s later again."""
    run, [p] = await _setup(api, db)
    for _ in range(2):  # two re-publishes: the next wait would be 900
        _advance(api, 901)
        await _tick(api)
        await api.mark_outbox_sent(db)
    assert (await _attempt(db, p["id"]))["publish_count"] == 3
    await api.result(p["id"], {"attempt": 1, "outcome": "poison"})
    assert (await api.resolve(run["id"], p["id"], "redrive")).status_code == 200
    await api.mark_outbox_sent(db)
    _advance(api, 360)
    await _tick(api)
    assert await _unsent(db) == []
    _advance(api, 1)
    await _tick(api)
    [row] = await _unsent(db)
    assert row["payload"]["data"]["republish_seq"] == 1


async def test_unsent_outbox_row_blocks(api: Api, db: Any) -> None:
    """PAY-407: no re-publish while an unsent outbox row exists for the attempt."""
    run = await api.approved_run(1)
    await _clock_at_last_publish(api, db)
    _advance(api, 5000)
    await _tick(api)
    assert len(await _unsent(db)) == 1  # only approve's own row
    assert (await api.payouts(run["id"]))[0]["attempts"][0]["status"] == "requested"


async def test_flushall_redrives_every_non_final_attempt(
    api: Api, db: Any, redis_admin: Any
) -> None:
    """PAY-407, PAY-NFR-03, plan §12.1 "Redis data loss": after FLUSHALL a tick re-publishes
    every live attempt (requested and indeterminate, same payout_id and attempt) and skips
    final and needs_operator ones; relayed, the stream holds them again."""
    _, ps = await _setup(api, db, 4)
    await api.result(ps[1]["id"], {"attempt": 1, "outcome": "indeterminate"})
    await api.result(ps[2]["id"], {"attempt": 1, "outcome": "settled", "bank_reference": "B"})
    await api.result(ps[3]["id"], {"attempt": 1, "outcome": "poison"})
    await redis_admin.flushall()
    _advance(api, 361)
    await _tick(api)
    rows = await _unsent(db)
    assert sorted((r["payout_id"], r["attempt"]) for r in rows) == sorted(
        [(ps[0]["id"], 1), (ps[1]["id"], 1)]
    )
    assert await redis_admin.xlen("stream:payout.requested") == 0
    relay = build_relay(
        api.rt.sessions, StreamProducer(api.app.state.ctx.redis), maxlen=1000, batch_size=10,
        poll_ms=10,
    )  # fmt: skip
    assert await relay.relay_once() == 2
    assert await redis_admin.xlen("stream:payout.requested") == 2
    assert await _unsent(db) == []


async def test_republish_racing_a_live_consumer(api: Api, db: Any) -> None:
    """PAY-NFR-02, plan §12.1 "Re-publish racing a live consumer": both event_ids pass the
    instruction check for the same attempt (same bank key); the first result applies and the
    second gets 200 with the stored outcome."""
    _, [p] = await _setup(api, db)
    _advance(api, 361)
    await _tick(api)
    events = await db.fetch("SELECT id FROM payroll.outbox WHERE payout_id = $1", p["id"])
    assert len(events) == 2
    url = f"/v1/internal/payouts/{p['id']}/instruction?attempt=1"
    for _ in events:
        assert (await api.client.get(url, headers=api.worker())).status_code == 200
    settled = {"attempt": 1, "outcome": "settled", "bank_reference": "BR"}
    first = await api.result(p["id"], settled, key=f"{events[0]['id']}:settled")
    second = await api.result(p["id"], settled, key=f"{events[1]['id']}:settled")
    assert (first.json()["outcome"], second.json()["outcome"]) == ("settled", "settled")
    assert "idempotent-replayed" not in second.headers
    applied = await db.fetchval(
        "SELECT count(*) FROM payroll.audit_events WHERE event = 'payout.result_recorded'"
    )
    assert applied == 1


async def test_ticks_cover_several_runs_and_skip_stale_attempts(api: Api, db: Any) -> None:
    """PAY-407, §4.10: one tick locks several runs in id order and re-publishes each current
    attempt; a retried payout's old attempt 1 is never re-published."""
    await api.create_employee(name="A", email="a@example.com")
    runs = []
    for month in ("2026-01-01", "2026-02-01"):
        run = await api.create_run("monthly", month)
        await api.calculate(run["id"], name="Carl Calculator")
        assert (await api.approve(run["id"])).status_code == 200
        runs.append(run)
    await api.mark_outbox_sent(db)
    p0 = (await api.payouts(runs[0]["id"]))[0]
    await api.result(p0["id"], {"attempt": 1, "outcome": "rejected", "reason": "x"})
    assert (await api.retry(runs[0]["id"], p0["id"])).status_code == 200
    await api.mark_outbox_sent(db)
    await _clock_at_last_publish(api, db)
    _advance(api, 361)
    await _tick(api)
    rows = await _unsent(db)
    assert sorted(r["attempt"] for r in rows) == [1, 2]
    assert all(r["payload"]["data"]["republish_seq"] == 1 for r in rows)
    assert {r["payload"]["data"]["pay_run_id"] for r in rows} == {runs[0]["id"], runs[1]["id"]}
