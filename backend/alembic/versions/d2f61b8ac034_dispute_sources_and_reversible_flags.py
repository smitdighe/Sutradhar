"""dispute sources, reversible fraud flags, and dispute clearing events

``items.dispute_status`` is a two-valued summary. It answers "is this contested"
and cannot answer "why", which is what makes it impossible to reverse safely: an
item disputed both because its registrant was fraud-flagged and because an
inspector found something wrong has two independent reasons, and clearing the
flag must lift the first while leaving the second standing. With one boolean,
lifting either erases both and the inspector's finding vanishes without trace.

``item_disputes`` records each reason separately, with the actor whose flag
caused it. Rows are never deleted -- lifting stamps ``cleared_at`` -- so the
history reads as "raised, then lifted, by whom, when". A partial unique index
allows one *open* dispute per (item, source) and any number of closed ones, so an
actor can be flagged, cleared and flagged again without collision.

Two enum members come with it. ``auth_event_type.FRAUD_CLEAR``: an append-only
audit log cannot un-say a flag, so a reversal is a second event rather than a
mutation of the first. ``item_event_type.DISPUTE_CLEARED``: a dispute that
appears and then silently vanishes is indistinguishable from one that never
happened, and a consumer who saw it deserves to see its resolution.

``ALTER TYPE ... ADD VALUE`` runs inside a transaction on PostgreSQL 12+; the new
values are added here and used only by later statements, which is the part that
stays forbidden.

Revision ID: d2f61b8ac034
Revises: c1a4e07d92b3
Create Date: 2026-08-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d2f61b8ac034"
down_revision: str | None = "c1a4e07d92b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DISPUTE_SOURCES = ("FRAUD_FLAG", "INSPECTION", "MANUAL")

NEW_AUTH_EVENT_TYPES = ("FRAUD_CLEAR",)
NEW_ITEM_EVENT_TYPES = ("DISPUTE_CLEARED",)

AUTH_EVENT_TYPES_BEFORE = (
    "REGISTER",
    "LOGIN_SUCCESS",
    "LOGIN_FAILURE",
    "REFRESH",
    "REFRESH_REUSE_DETECTED",
    "LOGOUT",
    "OAUTH_LINK",
    "OAUTH_NEW_ACCOUNT",
    "ROLE_GRANT",
    "FRAUD_FLAG",
)

ITEM_EVENT_TYPES_BEFORE = (
    "REGISTERED",
    "SPLIT",
    "ATTESTED",
    "ANCHORED",
    "DISPUTED",
    "CLAIMED",
    "REORGED",
    "ANCHOR_FAILED",
)


def upgrade() -> None:
    dispute_source = postgresql.ENUM(*DISPUTE_SOURCES, name="dispute_source")
    dispute_source.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "item_disputes",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "item_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source",
            postgresql.ENUM(*DISPUTE_SOURCES, name="dispute_source", create_type=False),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "triggered_by",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "raised_by",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "raised_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cleared_by",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # One *open* dispute per (item, source). Closed rows are unconstrained, so a
    # flag/clear/flag cycle produces three rows rather than a constraint error.
    op.create_index(
        "uq_item_disputes_open_per_source",
        "item_disputes",
        ["item_id", "source"],
        unique=True,
        postgresql_where=sa.text("cleared_at IS NULL"),
    )
    op.create_index(
        "ix_item_disputes_open",
        "item_disputes",
        ["item_id"],
        postgresql_where=sa.text("cleared_at IS NULL"),
    )
    op.create_index("ix_item_disputes_triggered_by", "item_disputes", ["triggered_by"])
    op.create_index("ix_item_disputes_raised_by", "item_disputes", ["raised_by"])
    op.create_index("ix_item_disputes_cleared_by", "item_disputes", ["cleared_by"])

    for value in NEW_AUTH_EVENT_TYPES:
        op.execute(f"ALTER TYPE auth_event_type ADD VALUE IF NOT EXISTS '{value}'")
    for value in NEW_ITEM_EVENT_TYPES:
        op.execute(f"ALTER TYPE item_event_type ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    """Drop the table and rebuild both enums without the new members.

    PostgreSQL cannot drop an enum value, so each type is recreated. Rows already
    carrying one of the new values cannot be cast into the smaller type, and
    rewriting them to something else would falsify an append-only audit log, so
    this refuses rather than guesses.
    """
    connection = op.get_bind()

    _refuse_if_used(connection, "auth_events", "auth_event_type", NEW_AUTH_EVENT_TYPES)
    _refuse_if_used(connection, "item_events", "item_event_type", NEW_ITEM_EVENT_TYPES)

    op.drop_index("ix_item_disputes_cleared_by", table_name="item_disputes")
    op.drop_index("ix_item_disputes_raised_by", table_name="item_disputes")
    op.drop_index("ix_item_disputes_triggered_by", table_name="item_disputes")
    op.drop_index("ix_item_disputes_open", table_name="item_disputes")
    op.drop_index("uq_item_disputes_open_per_source", table_name="item_disputes")
    op.drop_table("item_disputes")
    op.execute("DROP TYPE IF EXISTS dispute_source")

    _rebuild_enum(
        "auth_event_type", AUTH_EVENT_TYPES_BEFORE, [("auth_events", "event_type")]
    )
    _rebuild_enum(
        "item_event_type", ITEM_EVENT_TYPES_BEFORE, [("item_events", "event_type")]
    )


def _refuse_if_used(
    connection: sa.Connection, table: str, type_name: str, values: Sequence[str]
) -> None:
    rendered = ", ".join(f"'{value}'" for value in values)
    stranded = connection.execute(
        sa.text(f"SELECT count(*) FROM {table} WHERE event_type::text IN ({rendered})")
    ).scalar_one()
    if stranded:
        raise RuntimeError(
            f"{stranded} {table} row(s) use {tuple(values)}; downgrading would have to "
            f"rewrite an append-only audit log. Resolve those rows deliberately first."
        )


def _rebuild_enum(
    type_name: str, members: Sequence[str], columns: Sequence[tuple[str, str]]
) -> None:
    rendered = ", ".join(f"'{value}'" for value in members)
    op.execute(f"ALTER TYPE {type_name} RENAME TO {type_name}_old")
    op.execute(f"CREATE TYPE {type_name} AS ENUM ({rendered})")
    for table, column in columns:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} "
            f"TYPE {type_name} USING {column}::text::{type_name}"
        )
    op.execute(f"DROP TYPE {type_name}_old")
