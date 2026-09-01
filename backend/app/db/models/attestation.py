"""Third-party attestations about an item."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from app.core.clock import now
from app.db.base import Base
from app.db.models.enums import DISPUTE_SOURCE, USER_ROLE, DisputeSource, UserRole
from app.db.models.mixins import UUIDPrimaryKeyMixin

__all__ = ["Attestation", "ItemDispute"]


class Attestation(Base, UUIDPrimaryKeyMixin):
    """A signed statement by one party about one item.

    ``attestor_role`` snapshots the role held at attestation time. Roles change;
    an inspector's past attestation was still made as an inspector, and reading
    the role live off ``users`` would silently rewrite history.

    ``statement_hash`` is anchored on chain and recomputed from ``statement``
    at verification time.
    """

    __tablename__ = "attestations"

    item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("items.id", ondelete="CASCADE"), nullable=False
    )
    attestor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    attestor_role: Mapped[UserRole] = mapped_column(USER_ROLE, nullable=False)
    statement: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    statement_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now, server_default=func.now()
    )

    __table_args__ = (
        # One attestation per party per item; a change of mind is a new item
        # event, not a silent overwrite of the original statement.
        UniqueConstraint("item_id", "attestor_id", name="uq_attestations_item_attestor"),
        Index("ix_attestations_item_id_created_at", "item_id", "created_at"),
        Index("ix_attestations_attestor_id", "attestor_id"),
        Index("ix_attestations_statement_hash", "statement_hash"),
    )


class ItemDispute(Base, UUIDPrimaryKeyMixin):
    """One reason an item is currently contested.

    ``items.dispute_status`` is a two-valued summary and cannot answer *why*, so
    it cannot be reversed safely. An item disputed both because its registrant
    was fraud-flagged and because an inspector found something wrong has two
    independent reasons; clearing the flag must lift the first and leave the
    second standing. With a single boolean, lifting either erases both, and the
    inspector's finding disappears with no trace that it ever existed.

    Rows are never deleted. Lifting a dispute stamps ``cleared_at``, so the
    history reads as "raised, then lifted, by whom, when" rather than as a
    dispute that never happened.

    A partial unique index keeps one *open* dispute per (item, source) while
    allowing any number of closed ones -- an actor can be flagged, cleared and
    flagged again, and each cycle is its own row.
    """

    __tablename__ = "item_disputes"

    item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("items.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[DisputeSource] = mapped_column(DISPUTE_SOURCE, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # The actor whose flagging caused this dispute, for FRAUD_FLAG rows. This is
    # what makes clearing selective: a clear lifts exactly the disputes its own
    # flag raised, identified by this column rather than by guessing from dates.
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    raised_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    raised_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now, server_default=func.now()
    )
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cleared_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        # One open dispute per (item, source); closed ones are unconstrained.
        Index(
            "uq_item_disputes_open_per_source",
            "item_id",
            "source",
            unique=True,
            postgresql_where=text("cleared_at IS NULL"),
        ),
        # The hot read: "is this item disputed, and why", answered without
        # scanning the closed history.
        Index(
            "ix_item_disputes_open",
            "item_id",
            postgresql_where=text("cleared_at IS NULL"),
        ),
        Index("ix_item_disputes_triggered_by", "triggered_by"),
        Index("ix_item_disputes_raised_by", "raised_by"),
        Index("ix_item_disputes_cleared_by", "cleared_by"),
    )
