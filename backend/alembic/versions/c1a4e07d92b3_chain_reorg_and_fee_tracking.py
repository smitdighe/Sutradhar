"""chain reorg detection, replace-by-fee tracking, and the event mirror

Five things Phase 7 cannot be honest without.

``chain_txs.block_hash`` -- reorg detection re-reads the block at the recorded
height and compares hashes. Without the hash that was seen at mining time there
is nothing to compare against, and an orphaned anchor would sit in the database
claiming to be confirmed forever.

``chain_txs.max_fee_per_gas`` / ``max_priority_fee_per_gas`` -- a stuck
transaction is replaced at the same nonce with a fee bump computed from the
previous attempt's fee. That fee has to outlive the process that sent it.

``item_event_type`` gains ``REORGED`` and ``ANCHOR_FAILED`` -- an item that was
anchored and then un-anchored by a reorg is a distinct history, and recording it
as another ``ANCHORED`` row with a flag in the payload would make the timeline
read as a success.

``chain_outbox.error_chain`` -- a dead letter that carries only the last error
sends whoever reads it after a symptom instead of a cause.

``chain_events`` -- the observed side of the system. ``chain_txs`` is what this
system tried to do; ``chain_events`` is what the chain says happened.
Reconciliation is the diff between the two, and it would be meaningless if the
writer's own record doubled as the chain's. ``chain_txs.outbox_id`` also becomes
nullable here, because a gap fill is a real transaction with no business job
behind it and has to be recorded or the gap detector pays to fill the same hole
on every sweep.

``ALTER TYPE ... ADD VALUE`` runs inside a transaction on PostgreSQL 12+, which
is why this migration does not need ``autocommit_block``. The new values are only
added here, never used, and using a value added in the same transaction is the
part that is still forbidden.

Revision ID: c1a4e07d92b3
Revises: 7f93cea8632d
Create Date: 2026-08-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c1a4e07d92b3"
down_revision: str | None = "7f93cea8632d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_ITEM_EVENT_TYPES = ("REORGED", "ANCHOR_FAILED")

# Every member the type must have after downgrade, in creation order.
ITEM_EVENT_TYPES_BEFORE = (
    "REGISTERED",
    "SPLIT",
    "ATTESTED",
    "ANCHORED",
    "DISPUTED",
    "CLAIMED",
)


def upgrade() -> None:
    op.add_column("chain_txs", sa.Column("block_hash", sa.Text(), nullable=True))
    op.add_column("chain_txs", sa.Column("max_fee_per_gas", sa.BigInteger(), nullable=True))
    op.add_column(
        "chain_txs", sa.Column("max_priority_fee_per_gas", sa.BigInteger(), nullable=True)
    )
    # A gap fill is a real transaction with no business job behind it, and it
    # has to be recorded or the gap detector pays to fill the same hole on every
    # sweep.
    op.alter_column("chain_txs", "outbox_id", existing_type=sa.Uuid(), nullable=True)

    # A dead letter that carries only the last error sends whoever reads it
    # after a symptom instead of a cause.
    op.add_column(
        "chain_outbox",
        sa.Column(
            "error_chain",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    # The observed side of the system. chain_txs records what this system tried
    # to do; chain_events records what the chain says happened. Reconciliation
    # is the diff between them, and it would be meaningless if the writer's own
    # record doubled as the chain's.
    op.create_table(
        "chain_events",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("event_name", sa.Text(), nullable=False),
        sa.Column("tx_hash", sa.Text(), nullable=False),
        sa.Column("log_index", sa.Integer(), nullable=False),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        sa.Column("block_hash", sa.Text(), nullable=False),
        sa.Column("contract_address", sa.Text(), nullable=False),
        sa.Column("subject_hash", sa.Text(), nullable=False),
        sa.Column("issuer_hash", sa.Text(), nullable=True),
        sa.Column("issuer_address", sa.Text(), nullable=False),
        sa.Column("leaf_count", sa.Integer(), nullable=True),
        sa.Column("chain_timestamp", sa.BigInteger(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("tx_hash", "log_index", name="uq_chain_events_tx_log"),
    )
    op.create_index("ix_chain_events_subject_hash", "chain_events", ["subject_hash"])
    op.create_index("ix_chain_events_block_number", "chain_events", ["block_number"])
    op.create_index("ix_chain_events_event_name", "chain_events", ["event_name"])

    for value in NEW_ITEM_EVENT_TYPES:
        op.execute(f"ALTER TYPE item_event_type ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    """Remove the columns, and rebuild ``item_event_type`` without the new members.

    PostgreSQL cannot drop an enum value, so the type is recreated. Rows already
    carrying one of the new values cannot be cast into the smaller type, and
    rewriting them to something else would falsify an append-only audit log --
    so this refuses rather than guesses.
    """
    connection = op.get_bind()
    values = ", ".join(f"'{value}'" for value in NEW_ITEM_EVENT_TYPES)
    stranded = connection.execute(
        sa.text(f"SELECT count(*) FROM item_events WHERE event_type::text IN ({values})")
    ).scalar_one()
    if stranded:
        raise RuntimeError(
            f"{stranded} item_events row(s) use {NEW_ITEM_EVENT_TYPES}; "
            "downgrading would have to rewrite an append-only audit log. "
            "Resolve those rows deliberately before downgrading."
        )

    members = ", ".join(f"'{value}'" for value in ITEM_EVENT_TYPES_BEFORE)
    op.execute("ALTER TYPE item_event_type RENAME TO item_event_type_old")
    op.execute(f"CREATE TYPE item_event_type AS ENUM ({members})")
    op.execute(
        "ALTER TABLE item_events ALTER COLUMN event_type "
        "TYPE item_event_type USING event_type::text::item_event_type"
    )
    op.execute("DROP TYPE item_event_type_old")

    orphaned = connection.execute(
        sa.text("SELECT count(*) FROM chain_txs WHERE outbox_id IS NULL")
    ).scalar_one()
    if orphaned:
        raise RuntimeError(
            f"{orphaned} chain_txs row(s) are gap fills with no outbox job; "
            "restoring NOT NULL would require inventing a job for a real transaction."
        )
    op.alter_column("chain_txs", "outbox_id", existing_type=sa.Uuid(), nullable=False)

    op.drop_index("ix_chain_events_event_name", table_name="chain_events")
    op.drop_index("ix_chain_events_block_number", table_name="chain_events")
    op.drop_index("ix_chain_events_subject_hash", table_name="chain_events")
    op.drop_table("chain_events")

    op.drop_column("chain_outbox", "error_chain")
    op.drop_column("chain_txs", "max_priority_fee_per_gas")
    op.drop_column("chain_txs", "max_fee_per_gas")
    op.drop_column("chain_txs", "block_hash")
