"""Operational tables: rate limiting, idempotency, quota accounting, dead letters.

All four deliberately live in Postgres rather than Redis. Render's free tier is a
single instance, the database is already a hard dependency, and a second service
is one more thing that can be down during a demo. If throughput ever outgrows
this, the interfaces in :mod:`app.core` are what changes, not the callers.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from app.core.clock import now
from app.db.base import Base
from app.db.models.mixins import UUIDPrimaryKeyMixin

__all__ = ["DeadLetter", "IdempotencyKey", "RateLimitBucket", "QuotaUsage"]


class RateLimitBucket(Base):
    """One fixed window of one limiter.

    The composite primary key is the whole mechanism: an atomic
    ``INSERT ... ON CONFLICT DO UPDATE SET count = count + 1 RETURNING count``
    both creates the window and increments it, so concurrent requests cannot
    lose an update the way a read-then-write would.

    Fixed windows rather than a sliding log: a burst straddling a boundary can
    briefly pass at up to twice the limit, which is an acceptable trade for one
    row and one statement per check.
    """

    __tablename__ = "rate_limit_buckets"

    # e.g. 'login', 'oauth_start' -- which limiter.
    scope: Mapped[str] = mapped_column(Text, primary_key=True)
    # e.g. an IP hash, a user id, a pending-token jti -- who is limited.
    identifier: Mapped[str] = mapped_column(Text, primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_rate_limit_buckets_expires_at", "expires_at"),)


class IdempotencyKey(Base, UUIDPrimaryKeyMixin):
    """A recorded response, replayed when the same request arrives twice.

    ``request_hash`` is what makes this safe. A client retrying after a timeout
    must get the original response; a client reusing a key for a *different*
    request is a bug, and returning the old response would silently drop the new
    one, so that case is a 409 instead.
    """

    __tablename__ = "idempotency_keys"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_idempotency_keys_user_key"),
        # Keys expire after 24h; this index is what the cleanup job scans.
        Index("ix_idempotency_keys_created_at", "created_at"),
    )


class QuotaUsage(Base, UUIDPrimaryKeyMixin):
    """Consumption of one metered external resource over one period.

    Periodic quotas (Alchemy compute units, monthly) get one row per period.
    Cumulative quotas (Pinata storage bytes) pin ``period_start`` to the Unix
    epoch and keep a single row forever. Keeping past periods rather than
    resetting in place means last month's burn is still auditable after the
    rollover.

    ``numeric`` rather than ``bigint`` because these are budgets that get
    divided and compared, and mixing integer division into a budget check is
    how a limiter ends up off by one at the ceiling.
    """

    __tablename__ = "quota_usage"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used: Mapped[Decimal] = mapped_column(Numeric(28, 4), nullable=False, default=Decimal(0))
    budget: Mapped[Decimal] = mapped_column(Numeric(28, 4), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=now,
        onupdate=now,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("name", "period_start", name="uq_quota_usage_name_period"),
        Index("ix_quota_usage_name", "name"),
    )


class DeadLetter(Base, UUIDPrimaryKeyMixin):
    """A job that exhausted its retries, parked for a human to look at.

    Nothing is silently dropped. ``error_chain`` keeps the full failure history
    because the last error is usually a symptom of the first.
    """

    __tablename__ = "dead_letters"

    source: Mapped[str] = mapped_column(Text, nullable=False)
    original_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    error_chain: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index("ix_dead_letters_source_created_at", "source", "created_at"),
        Index("ix_dead_letters_resolved_at", "resolved_at"),
        Index("ix_dead_letters_resolved_by", "resolved_by"),
    )
