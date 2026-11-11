"""The audit event and role lists agree everywhere they're spelled out (identity LLD §2.2, §2.6)."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

from identity.db import models
from identity.domain.audit_events import ALL_EVENTS
from identity.domain.roles import ALL_ROLES

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0001_initial.py"


def _quoted(sql: str) -> list[str]:
    return re.findall(r"'([^']+)'", sql)


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("identity_migration_0001", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_audit_events_match_the_check_constraint() -> None:
    """PAY-109, PAY-NFR-08: ck_audit_log_event in the models and migration 0001 lists exactly
    the events of domain/audit_events.py."""
    assert _quoted(models.AUDIT_EVENTS_SQL) == list(ALL_EVENTS)
    assert _quoted(_migration().AUDIT_EVENTS_SQL) == list(ALL_EVENTS)


def test_roles_match_the_check_constraint() -> None:
    """PAY-101, PAY-108: ck_users_roles allows exactly the three roles of ADR 0002."""
    expected = sorted(str(r) for r in ALL_ROLES)
    assert sorted(_quoted(models.ROLES_SQL)) == expected
    assert sorted(_quoted(_migration().ROLES_SQL)) == expected
