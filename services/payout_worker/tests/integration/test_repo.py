"""DisbursementRepo on real Postgres: a definite bank answer is never overwritten (design §2.1,
§3.1.1; PAY-403, R7, C63)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from payout_worker.db.repo import DisbursementRepo, StoredAnswer
from pycommon.db import create_engine, session_factory
from pycommon.testing import PgBootstrapped, RedisAcl

from tests.helpers import BANK_REF, envelope
from tests.integration.conftest import STREAM, worker_settings

ROW = "SELECT status, outcome, reason, bank_reference FROM payouts.disbursements"


async def test_definite_answer_survives_later_tries(
    admin_db: Any, migrated: PgBootstrapped, redis_acl: RedisAcl
) -> None:
    """C63: once settled is recorded for an attempt, a new event's upsert, an indeterminate
    record and an attempt_not_current finish keep it, so it can still be reported."""
    engine = create_engine(worker_settings(migrated, redis_acl))
    repo = DisbursementRepo(session_factory(engine), stream=STREAM)
    try:
        first = envelope()
        await repo.upsert_submitting(first)
        assert await repo.definite_answer(first.data.payout_id, 1) is None
        await repo.record_answer(first.data.payout_id, 1, "indeterminate", None, None)
        assert await repo.definite_answer(first.data.payout_id, 1) is None
        await repo.record_answer(first.data.payout_id, 1, "settled", None, BANK_REF)

        second = envelope(event_id=uuid4())
        await repo.upsert_submitting(second)
        await repo.record_answer(second.data.payout_id, 1, "indeterminate", None, None)
        assert await repo.definite_answer(second.data.payout_id, 1) == StoredAnswer(
            "settled", None, BANK_REF
        )
        await repo.finish(second, "attempt_not_current")
        row = await admin_db.fetchrow(ROW)
        assert dict(row) == {
            "status": "done",
            "outcome": "settled",
            "reason": None,
            "bank_reference": BANK_REF,
        }
        processed = await admin_db.fetchval(
            "SELECT outcome FROM payouts.processed_events WHERE event_id = $1", second.event_id
        )
        assert processed == "attempt_not_current"
    finally:
        await engine.dispose()


async def test_non_definite_outcomes_are_still_cleared(
    admin_db: Any, migrated: PgBootstrapped, redis_acl: RedisAcl
) -> None:
    """Design §2.1: a non-definite outcome (poison, indeterminate) is cleared by the next
    event's upsert, as before."""
    engine = create_engine(worker_settings(migrated, redis_acl))
    repo = DisbursementRepo(session_factory(engine), stream=STREAM)
    try:
        env = envelope()
        await repo.upsert_submitting(env)
        await repo.record_answer(env.data.payout_id, 1, "poison", "amount_mismatch", None)
        await repo.upsert_submitting(envelope(event_id=uuid4()))
        row = await admin_db.fetchrow(ROW)
        assert (row["status"], row["outcome"], row["reason"]) == ("submitting", None, None)
        await repo.record_answer(env.data.payout_id, 1, "rejected", "account_closed", None)
        await repo.finish(env, "poison", reason="key_window_expired")
        row = await admin_db.fetchrow(ROW)
        assert (row["outcome"], row["reason"]) == ("rejected", "account_closed")
    finally:
        await engine.dispose()
