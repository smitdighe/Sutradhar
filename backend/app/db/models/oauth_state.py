"""Single-use OAuth authorization-request nonces.

The ``state`` parameter is signed, which proves this server minted it, but a
signature says nothing about whether it has been used before. A replayable state
is still a CSRF primitive: an attacker who captures one callback URL can replay
it to graft their provider identity onto whoever clicks it.

So each nonce is claimed exactly once, by an atomic
``INSERT ... ON CONFLICT DO NOTHING RETURNING nonce``. Zero rows back means
somebody already used it.

A dedicated table rather than ``rate_limit_buckets``: those windows are
epoch-aligned, so a state minted at 12:09:59 and replayed at 12:10:01 would land
in a fresh bucket and the replay would succeed.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.clock import now
from app.db.base import Base

__all__ = ["OAuthState"]


class OAuthState(Base):
    """One consumed authorization-request nonce."""

    __tablename__ = "oauth_states"

    # The nonce itself is the primary key, which is what makes the insert the
    # claim: uniqueness is enforced by the index, not by application logic.
    nonce: Mapped[str] = mapped_column(String(64), primary_key=True)
    consumed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now, server_default=func.now()
    )
    # Rows are useless past the state TTL; this index is what a cleanup job scans.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_oauth_states_expires_at", "expires_at"),)
