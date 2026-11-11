"""Alembic env for bank-stub (design §2.2; PAY-606): pycommon's helper does all the work."""

from bank_stub.db.models import metadata
from pycommon.db import run_alembic_env

run_alembic_env(metadata, schema="bank_stub", runtime_role="bank_stub_app")
