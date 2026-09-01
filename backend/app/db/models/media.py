"""Uploaded media, its pinning state, and the items it depicts."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from app.core.clock import now
from app.db.base import Base
from app.db.models.enums import MEDIA_KIND, PIN_STATUS, MediaKind, PinStatus
from app.db.models.mixins import UUIDPrimaryKeyMixin

__all__ = ["ItemMedia", "Media"]


class Media(Base, UUIDPrimaryKeyMixin):
    """One uploaded file, content-addressed by SHA-256.

    SHA-256 rather than keccak256 here because this is content addressing for
    IPFS, not a value anchored on chain -- ``cid`` derives from the same digest
    family IPFS uses. Deduplication is free: re-uploading identical bytes hits
    the unique index.

    Three storage locations, any of which may be null:

    * ``blob``        -- inline bytes, used for the offline demo path
    * ``mirror_path`` -- the local ``media_mirror/`` copy
    * ``cid``         -- the IPFS CID, once pinning succeeds
    """

    __tablename__ = "media"

    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    cid: Mapped[str | None] = mapped_column(Text, nullable=True)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    mirror_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    blob: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    pin_status: Mapped[PinStatus] = mapped_column(
        PIN_STATUS, nullable=False, default=PinStatus.PIN_PENDING
    )
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_media_pin_status", "pin_status"),
        Index("ix_media_uploaded_by", "uploaded_by"),
        Index("ix_media_cid", "cid"),
    )


class ItemMedia(Base):
    """Join between an item and a piece of media, tagged with what it shows.

    ``media`` is RESTRICT: the same blob can back several items, so deleting an
    item must not take shared bytes with it.
    """

    __tablename__ = "item_media"

    item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("items.id", ondelete="CASCADE"),
        primary_key=True,
    )
    media_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("media.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    kind: Mapped[MediaKind] = mapped_column(MEDIA_KIND, nullable=False)

    __table_args__ = (
        Index("ix_item_media_media_id", "media_id"),
        Index("ix_item_media_item_id_kind", "item_id", "kind"),
    )
