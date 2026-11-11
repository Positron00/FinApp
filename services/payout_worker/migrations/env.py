"""Alembic env for payout-worker (design §2.2; PAY-606): pycommon's helper does all the work."""

from payout_worker.db.models import metadata
from pycommon.db import run_alembic_env

run_alembic_env(metadata, schema="payouts", runtime_role="payouts_app")
