"""Alembic env for payroll (design §2.11; PAY-606): pycommon's helper does all the work."""

from payroll.db.models import Base
from pycommon.db import run_alembic_env

run_alembic_env(Base.metadata, schema="payroll", runtime_role="payroll_app")
