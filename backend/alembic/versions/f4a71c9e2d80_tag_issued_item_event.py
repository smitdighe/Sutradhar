"""record tag issuance as its own item event

Binding a physical tag to an item is a provenance event, not a column update.
``items.tag_code`` says which label is on the object right now; it cannot say
when the label was printed or who authorised it, and those are exactly the
questions a disputed tag raises.

``ALTER TYPE ... ADD VALUE`` cannot run inside a transaction block on
PostgreSQL below 12, and Alembic wraps migrations in one. Every supported
target here is 14+, where it is allowed, so this stays a plain ``op.execute``
rather than an autocommit block.

Revision ID: f4a71c9e2d80
Revises: e3b9c0d15f27
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f4a71c9e2d80"
down_revision: str | None = "e3b9c0d15f27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_EVENT_TYPES = ("TAG_ISSUED",)

EVENT_TYPES_BEFORE = (
    "REGISTERED",
    "SPLIT",
    "ATTESTED",
    "ANCHORED",
    "DISPUTED",
    "CLAIMED",
    "REORGED",
    "ANCHOR_FAILED",
    "DISPUTE_CLEARED",
)


def upgrade() -> None:
    for value in NEW_EVENT_TYPES:
        # IF NOT EXISTS because a database built from Base.metadata rather than
        # by replaying migrations already carries the member.
        op.execute(f"ALTER TYPE item_event_type ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    """Rebuild the enum without ``TAG_ISSUED``.

    Refuses while any event still carries it. The alternative would be to
    rewrite those rows to some other type, which would put a false statement
    into an append-only log -- the one table in this schema that exists
    precisely because its contents are never rewritten.
    """
    import sqlalchemy as sa

    connection = op.get_bind()
    rendered = ", ".join(f"'{value}'" for value in NEW_EVENT_TYPES)
    stranded = connection.execute(
        sa.text(f"SELECT count(*) FROM item_events WHERE event_type::text IN ({rendered})")
    ).scalar_one()
    if stranded:
        raise RuntimeError(
            f"{stranded} item_event row(s) are {NEW_EVENT_TYPES}; downgrading would have to "
            "rewrite entries in an append-only log. Remove those events first if you mean it."
        )

    members = ", ".join(f"'{value}'" for value in EVENT_TYPES_BEFORE)
    op.execute("ALTER TYPE item_event_type RENAME TO item_event_type_old")
    op.execute(f"CREATE TYPE item_event_type AS ENUM ({members})")
    op.execute(
        "ALTER TABLE item_events ALTER COLUMN event_type "
        "TYPE item_event_type USING event_type::text::item_event_type"
    )
    op.execute("DROP TYPE item_event_type_old")
