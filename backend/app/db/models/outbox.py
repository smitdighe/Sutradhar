"""The durable job queue. Postgres is the broker.

Named ``outbox`` rather than ``chain_outbox`` because it is not chain-specific:
the transactional-outbox pattern is what lets a business change and the
background work it implies commit in the *same transaction*, and that is worth
having for IPFS pinning as much as for chain anchoring. Publishing to a broker
and committing to Postgres are two writes, and every ordering of those two has a
crash window that either loses the job or invents one.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.clock import now
from app.db.base import Base
from app.db.models.enums import (
    OUTBOX_JOB_TYPE,
    OUTBOX_STATUS,
    OutboxJobType,
    OutboxStatus,
)
from app.db.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

__all__ = ["Outbox"]


class Outbox(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One durable background job: claimed, retried, and never silently dropped.

    Not chain-specific, despite where it started. This is the single mechanism
    for any work that has to survive a crash -- anchoring a hash, pinning a file
    to IPFS, anything that talks to a service which can be down for hours.
    Claimed under ``FOR UPDATE SKIP LOCKED``, retried with jittered exponential
    backoff, and parked in ``dead_letters`` with its full error history rather
    than disappearing.

    A second copy of this machinery for a second kind of job would drift from
    the first, and the drift would show up as one queue silently not retrying.
    So job types are dispatched by separate drains reading the same table, each
    filtering to the types it understands.
    """

    __tablename__ = "outbox"

    job_type: Mapped[OutboxJobType] = mapped_column(OUTBOX_JOB_TYPE, nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    # Makes enqueue idempotent: the same logical anchor can be requested twice
    # without producing two transactions.
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now, server_default=func.now()
    )
    # Lease fields: a worker claims a row by stamping these, so a crashed worker
    # releases its rows once the lease goes stale rather than wedging them.
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Every attempt's failure, not just the most recent one. The last error is
    # usually a symptom -- "nonce too low" -- and the first is the cause -- "the
    # RPC was unreachable for six minutes". A dead letter carrying only the
    # symptom sends whoever reads it to the wrong place.
    #
    # Always reassigned, never mutated in place: a plain JSONB column has no
    # change tracking, and ``row.error_chain.append(...)`` would be silently
    # dropped at flush.
    error_chain: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    status: Mapped[OutboxStatus] = mapped_column(
        OUTBOX_STATUS, nullable=False, default=OutboxStatus.QUEUED
    )

    __table_args__ = (
        # The worker's claim query: WHERE status = 'QUEUED' AND next_attempt_at <= now()
        Index("ix_outbox_status_next_attempt_at", "status", "next_attempt_at"),
        Index("ix_outbox_locked_at", "locked_at"),
    )
