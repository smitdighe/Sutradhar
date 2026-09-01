"""GI categories, the items registered under them, and their provenance events."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.core.clock import now
from app.db.base import Base
from app.db.models.enums import (
    DISPUTE_STATUS,
    ITEM_EVENT_TYPE,
    ITEM_STATUS,
    DisputeStatus,
    ItemEventType,
    ItemStatus,
)
from app.db.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

__all__ = ["GICategory", "Item", "ItemEvent"]


class GICategory(Base, UUIDPrimaryKeyMixin):
    """A Geographical Indication category and the schema its items must satisfy.

    ``attribute_schema`` is a JSON Schema Draft 2020-12 document validated
    against at item registration. Schemas evolve, so each row is versioned and
    an item pins the version it was written under -- re-validating an old item
    against a new schema would retroactively invalidate honest records.
    """

    __tablename__ = "gi_categories"

    # NOT unique on its own: a category has many versions, all sharing a
    # slug. Uniqueness lives on (slug, schema_version) in __table_args__.
    # A column-level unique here would make v2 impossible to insert.
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    is_textile: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    attribute_schema: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 'metre', 'pair', 'piece' -- the unit items in this category are counted in.
    quantity_unit: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("slug", "schema_version", name="uq_gi_categories_slug_version"),
        Index("ix_gi_categories_slug", "slug"),
        Index("ix_gi_categories_created_by", "created_by"),
        Index("ix_gi_categories_is_active", "is_active"),
    )


class Item(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A registered textile, or a piece split from a larger one.

    ``item_hash`` is the value anchored on chain. Verification recomputes it
    from this row via :func:`app.core.hashing.hash_object` and compares.
    """

    __tablename__ = "items"

    category_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("gi_categories.id", ondelete="RESTRICT"), nullable=False
    )
    # Pinned at write time -- see GICategory.attribute_schema.
    category_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # A bolt split into saris: RESTRICT so a parent cannot vanish from under
    # its children and orphan a provenance chain.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("items.id", ondelete="RESTRICT"), nullable=True
    )
    registered_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    attributes: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    quantity_unit: Mapped[str] = mapped_column(Text, nullable=False)
    item_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # Null until a physical tag is printed and bound to this item.
    tag_code: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    status: Mapped[ItemStatus] = mapped_column(
        ITEM_STATUS, nullable=False, default=ItemStatus.PENDING
    )
    dispute_status: Mapped[DisputeStatus] = mapped_column(
        DISPUTE_STATUS, nullable=False, default=DisputeStatus.NONE
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    events: Mapped[list[ItemEvent]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # An item cannot be its own parent. Belt to the depth guard's braces in
        # the recursive CTEs: this makes the one-node cycle structurally
        # impossible, and the ceiling in tree.py terminates longer ones.
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="no_self_parent"),
        Index("ix_items_parent_id", "parent_id"),
        Index("ix_items_registered_by", "registered_by"),
        Index("ix_items_tag_code", "tag_code"),
        Index("ix_items_category_id_created_at", "category_id", "created_at"),
        Index("ix_items_status", "status"),
        Index("ix_items_dispute_status", "dispute_status"),
        Index("ix_items_created_at_id", "created_at", "id"),
    )


class ItemEvent(Base, UUIDPrimaryKeyMixin):
    """Append-only provenance event. Rows are never updated or deleted.

    ``actor_id`` is nulled rather than blocking user deletion: the event is the
    record that matters, and ``payload`` already carries what was asserted.
    """

    __tablename__ = "item_events"

    item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("items.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[ItemEventType] = mapped_column(ITEM_EVENT_TYPE, nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    payload_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now, server_default=func.now()
    )

    item: Mapped[Item] = relationship(back_populates="events")

    __table_args__ = (
        Index("ix_item_events_item_id_created_at", "item_id", "created_at"),
        Index("ix_item_events_actor_id", "actor_id"),
        Index("ix_item_events_event_type", "event_type"),
    )
