"""rename chain_outbox to outbox and add the PIN_MEDIA job type

The outbox was never chain-specific. It is the transactional-outbox pattern:
a business change and the background work it implies commit in the *same*
transaction, so a crash between them cannot lose the job or invent one. That is
worth exactly as much for pinning a file to IPFS -- a network call to a third
party that can be down for hours -- as it is for anchoring a hash.

Phase 9 gives it a second user, so the table stops claiming to be about chains.
The alternative was a second table with the same columns and a second copy of
the claim/backoff/dead-letter logic, which is how two queues end up disagreeing
about whether they retry.

``chain_txs`` keeps its name and its ``outbox_id`` foreign key: transactions
really are chain-specific.

Renaming a table renames its constraints and indexes in PostgreSQL only if they
were named after it, which these were, so each is renamed explicitly to keep the
schema readable in psql rather than leaving ``ix_chain_outbox_*`` on a table
called ``outbox``.

Revision ID: e3b9c0d15f27
Revises: d2f61b8ac034
Create Date: 2026-08-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "e3b9c0d15f27"
down_revision: str | None = "d2f61b8ac034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_JOB_TYPES = ("PIN_MEDIA",)

JOB_TYPES_BEFORE = ("ANCHOR_ITEM", "ANCHOR_ATTESTATION", "ANCHOR_BATCH")

# (old name, new name) for everything PostgreSQL derived from the table name.
RENAMES: tuple[tuple[str, str], ...] = (
    ("pk_chain_outbox", "pk_outbox"),
    ("uq_chain_outbox_dedupe_key", "uq_outbox_dedupe_key"),
    ("ix_chain_outbox_status_next_attempt_at", "ix_outbox_status_next_attempt_at"),
    ("ix_chain_outbox_locked_at", "ix_outbox_locked_at"),
)


def upgrade() -> None:
    op.rename_table("chain_outbox", "outbox")

    for old, new in RENAMES:
        # IF EXISTS because a database built from Base.metadata rather than by
        # replaying migrations may already carry the new names.
        op.execute(f"ALTER INDEX IF EXISTS {old} RENAME TO {new}")

    op.execute(
        "ALTER TABLE chain_txs RENAME CONSTRAINT "
        "fk_chain_txs_outbox_id_chain_outbox TO fk_chain_txs_outbox_id_outbox"
    )

    for value in NEW_JOB_TYPES:
        op.execute(f"ALTER TYPE outbox_job_type ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    """Rename back, and rebuild the job-type enum without ``PIN_MEDIA``.

    Refuses if any row still carries a job type that would not survive the
    smaller enum. Rewriting those rows to something else would either drop a
    pending pin silently or claim a media job was a chain job.
    """
    import sqlalchemy as sa

    connection = op.get_bind()
    rendered = ", ".join(f"'{value}'" for value in NEW_JOB_TYPES)
    stranded = connection.execute(
        sa.text(f"SELECT count(*) FROM outbox WHERE job_type::text IN ({rendered})")
    ).scalar_one()
    if stranded:
        raise RuntimeError(
            f"{stranded} outbox row(s) use {NEW_JOB_TYPES}; downgrading would have to "
            "discard or misdescribe them. Drain or resolve those jobs first."
        )

    op.execute(
        "ALTER TABLE chain_txs RENAME CONSTRAINT "
        "fk_chain_txs_outbox_id_outbox TO fk_chain_txs_outbox_id_chain_outbox"
    )
    for old, new in RENAMES:
        op.execute(f"ALTER INDEX IF EXISTS {new} RENAME TO {old}")

    op.rename_table("outbox", "chain_outbox")

    members = ", ".join(f"'{value}'" for value in JOB_TYPES_BEFORE)
    op.execute("ALTER TYPE outbox_job_type RENAME TO outbox_job_type_old")
    op.execute(f"CREATE TYPE outbox_job_type AS ENUM ({members})")
    op.execute(
        "ALTER TABLE chain_outbox ALTER COLUMN job_type "
        "TYPE outbox_job_type USING job_type::text::outbox_job_type"
    )
    op.execute("DROP TYPE outbox_job_type_old")
