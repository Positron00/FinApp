"""bank_stub.transfers: the persistent idempotent ledger (design §2.1, §2.2; PAY-412, R6, R13).

Revision ID: 0001_bank_stub_transfers
Revises:
Create Date: 2026-11-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_bank_stub_transfers"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE bank_stub.transfers (
          id               uuid        PRIMARY KEY,
          idempotency_key  text        NOT NULL
                             CHECK (idempotency_key ~ '^[A-Za-z0-9:_-]{1,255}$'),
          amount_cents     bigint      NOT NULL
                             CHECK (amount_cents > 0 AND amount_cents <= 1000000000),
          currency         char(3)     NOT NULL CHECK (currency = 'USD'),
          account_last4    char(4)     NOT NULL CHECK (account_last4 ~ '^[0-9]{4}$'),
          status           text        NOT NULL CHECK (status IN ('settled', 'rejected')),
          reason           text        NULL
                             CHECK (reason IN ('account_closed', 'insufficient_funds_at_bank')),
          bank_reference   text        NULL CHECK (bank_reference ~ '^BSR-[0-9A-F]{16}$'),
          created_at       timestamptz NOT NULL DEFAULT now(),
          updated_at       timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT transfers_idempotency_key_uq UNIQUE (idempotency_key),
          CONSTRAINT transfers_outcome_shape CHECK (
            (status = 'settled'  AND reason IS NULL     AND bank_reference IS NOT NULL) OR
            (status = 'rejected' AND reason IS NOT NULL AND bank_reference IS NULL))
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE bank_stub.transfers")
