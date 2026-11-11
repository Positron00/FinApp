"""Seed company_settings for COMPANY_ID: tax 0, retirement 0, column defaults otherwise.

Data migration (design §2.11; PAY-200, R22, C28). Idempotent: an existing row is kept.

Revision ID: 0002_seed_company_settings
Revises: 0001_initial_schema
Create Date: 2026-11-10
"""

import os
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_seed_company_settings"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    company_id = os.environ["COMPANY_ID"]  # KeyError: the migration fails loudly
    uuid.UUID(company_id)  # validates the format
    name = os.environ.get("PAYROLL_SEED_COMPANY_NAME", "PayRun Demo Company")
    op.execute(
        sa.text(
            "INSERT INTO payroll.company_settings (company_id, name, tax_rate_bps, retirement_bps) "
            "VALUES (CAST(:cid AS uuid), :name, 0, 0) ON CONFLICT (company_id) DO NOTHING"
        ).bindparams(cid=company_id, name=name)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM payroll.company_settings c WHERE c.company_id = CAST(:cid AS uuid) "
            "AND NOT EXISTS (SELECT 1 FROM payroll.employees e WHERE e.company_id = c.company_id)"
        ).bindparams(cid=os.environ["COMPANY_ID"])
    )
