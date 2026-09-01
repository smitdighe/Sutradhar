"""Public scan telemetry and one-time consumer ownership claims.

Privacy floor, enforced by the schema itself: **no raw IP address, no GPS, and
no granularity finer than a region.** A tag-abuse signal needs to
know that one tag was scanned in four states in a day; it does not need to know
which street. What is not stored cannot leak and cannot be subpoenaed, and the
anomaly heuristics in :mod:`app.core` are designed around that limit rather
than asking for it to be relaxed later.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from app.core.clock import now
from app.db.base import Base
from app.db.models.enums import SUSPICION_LEVEL, SuspicionLevel
from app.db.models.mixins import UUIDPrimaryKeyMixin

__all__ = ["Claim", "Scan"]


class Scan(Base, UUIDPrimaryKeyMixin):
    """One public scan of a tag code."""

    __tablename__ = "scans"

    item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("items.id", ondelete="CASCADE"), nullable=False
    )
    # Denormalised: a scan of an unknown or retired code is still worth keeping,
    # and this is what the scanner actually presented.
    tag_code: Mapped[str] = mapped_column(Text, nullable=False)
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    # ISO 3166-2 subdivision. This is the finest location granularity stored.
    region_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    device_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    suspicion_level: Mapped[SuspicionLevel] = mapped_column(
        SUSPICION_LEVEL, nullable=False, default=SuspicionLevel.NONE
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_scans_item_id_created_at", "item_id", "created_at"),
        Index("ix_scans_tag_code", "tag_code"),
        Index("ix_scans_suspicion_level", "suspicion_level"),
        Index("ix_scans_created_at_id", "created_at", "id"),
    )


class Claim(Base):
    """A consumer claiming the item they hold.

    ``item_id`` is the primary key, so one claim per item is a structural
    guarantee rather than a race the application has to win.
    """

    __tablename__ = "claims"

    item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("items.id", ondelete="CASCADE"),
        primary_key=True,
    )
    device_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    region_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now, server_default=func.now()
    )

    __table_args__ = (Index("ix_claims_claimed_at", "claimed_at"),)
