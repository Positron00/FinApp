"""The dual-approval denial audit never masks the 403 (design §4.4; PAY-401, PAY-500, C73)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

from payroll.domain import ApproverMustDiffer
from payroll.services.dual_denied import record_dual_denied
from pycommon.testing import JsonLogCapture


def _broken_sessions() -> Any:
    raise ConnectionError("db down")


async def test_a_failed_audit_write_is_logged_and_swallowed(caplog_json: JsonLogCapture) -> None:
    """PAY-401, C73: when the denial's own audit transaction fails, record_dual_denied logs
    audit.write_failed (audit_event, entity, error class) and returns: the caller's 403 stands."""
    rt = cast(Any, SimpleNamespace(sessions=_broken_sessions))
    principal = cast(Any, SimpleNamespace(cid=uuid4(), sub="alice"))
    run_id = uuid4()
    exc = ApproverMustDiffer(rule="creator", employee_ids=[])
    await record_dual_denied(
        rt, principal, exc, action="approve", entity_id=run_id, pay_run_id=run_id
    )
    [failed] = caplog_json.events("audit.write_failed")
    assert failed["audit_event"] == "payrun.approve_denied"
    assert failed["entity_id"] == str(run_id)
    assert failed["error"] == "DbUnavailable"  # pycommon maps the driver error
    assert "db down" not in caplog_json.text
