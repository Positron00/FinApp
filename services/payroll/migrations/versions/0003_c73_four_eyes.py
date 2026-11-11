"""C73: payout_attempts.resolved_by, and payroll.audit_events append-only for payroll_app.

- ``payout_attempts.resolved_by`` records the operator who last resolved the attempt (PAY-502).
  With dual approval on, that operator can't retry a payout they marked rejected (PAY-500).
- The runtime role may append to ``payroll.audit_events`` but never rewrite, delete or truncate it
  (as identity does for ``identity.audit_log``, C50). No payroll code updates or deletes audit
  rows, and no retention task purges them. ``20-payroll.sql`` re-revokes after its grants, so a
  bootstrap re-run can't restore them.

The runtime role's name is fixed per service, as in ``migrations/env.py`` (``payroll_app``).

Revision ID: 0003_c73_four_eyes
Revises: 0002_seed_company_settings
Create Date: 2026-11-11
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_c73_four_eyes"
down_revision: str | None = "0002_seed_company_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "payroll_app"


def upgrade() -> None:
    op.execute("ALTER TABLE payroll.payout_attempts ADD COLUMN resolved_by text NULL")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON payroll.audit_events FROM {RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute(f"GRANT UPDATE, DELETE ON payroll.audit_events TO {RUNTIME_ROLE}")
    op.execute("ALTER TABLE payroll.payout_attempts DROP COLUMN resolved_by")
