"""payouts.disbursements and payouts.processed_events (design §2.1, §2.2; PAY-403, R13, C22).

Revision ID: 0001_payouts_initial
Revises:
Create Date: 2026-11-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_payouts_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE payouts.disbursements (
          id              uuid        PRIMARY KEY,
          payout_id       uuid        NOT NULL,
          attempt         smallint    NOT NULL CHECK (attempt >= 1),
          company_id      uuid        NOT NULL,
          status          text        NOT NULL CHECK (status IN ('submitting', 'done')),
          outcome         text        NULL CHECK (outcome IN
                            ('settled', 'rejected', 'indeterminate', 'poison',
                             'attempt_not_current')),
          reason          text        NULL CHECK (char_length(reason) <= 100),
          bank_reference  text        NULL CHECK (char_length(bank_reference) <= 64),
          last_event_id   uuid        NOT NULL,
          deliveries      integer     NOT NULL DEFAULT 1 CHECK (deliveries >= 1),
          created_at      timestamptz NOT NULL DEFAULT now(),
          updated_at      timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT disbursements_payout_attempt_uq UNIQUE (payout_id, attempt),
          CONSTRAINT disbursements_done_has_outcome CHECK (status <> 'done' OR outcome IS NOT NULL)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE payouts.processed_events (
          event_id      uuid        PRIMARY KEY,
          stream        text        NOT NULL,
          payout_id     uuid        NOT NULL,
          attempt       smallint    NOT NULL CHECK (attempt >= 1),
          company_id    uuid        NOT NULL,
          outcome       text        NOT NULL CHECK (outcome IN
                          ('settled', 'rejected', 'indeterminate', 'poison',
                           'attempt_not_current')),
          processed_at  timestamptz NOT NULL DEFAULT now(),
          created_at    timestamptz NOT NULL DEFAULT now(),
          updated_at    timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX processed_events_processed_at_idx ON payouts.processed_events (processed_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE payouts.processed_events")
    op.execute("DROP TABLE payouts.disbursements")
