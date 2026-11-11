"""Alembic env for schema ``identity`` (identity LLD §2.9; PAY-606)."""

from identity.db.models import Base
from pycommon.db import run_alembic_env

run_alembic_env(Base.metadata, schema="identity", runtime_role="identity_app")
