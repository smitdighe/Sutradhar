"""Column conventions applied to every table.

The Python-side ``default`` routes through :func:`app.core.clock.now`, so a test
that freezes the clock also freezes what gets written. The matching
``server_default`` covers rows inserted by raw SQL, migrations, or a psql
session, where no Python default runs.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from app.core.clock import now
from app.core.ids import new_uuid

__all__ = ["TimestampMixin", "UUIDPrimaryKeyMixin", "utcnow_column"]


def utcnow_column(on_update: bool = False) -> Mapped[datetime]:
    """A non-nullable ``timestamptz`` defaulting to the current instant."""
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=now,
        onupdate=now if on_update else None,
        server_default=func.now(),
    )


class UUIDPrimaryKeyMixin:
    """``id`` primary key holding a UUIDv7 as a native ``uuid``.

    UUIDv7 is time-ordered, so inserts stay local in the B-tree instead of
    scattering the way UUIDv4 does, and ``ORDER BY id`` matches insertion order
    closely enough to break ties in keyset pagination.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=new_uuid
    )


class TimestampMixin:
    """``created_at`` / ``updated_at``, both non-nullable ``timestamptz``."""

    created_at: Mapped[datetime] = utcnow_column()
    updated_at: Mapped[datetime] = utcnow_column(on_update=True)
