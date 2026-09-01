"""Transactional outbox, transaction tracking, nonce allocation, and Merkle batches.

Writes to Postgres and writes to a chain cannot share a transaction, so the
service never calls the chain inline. It commits an outbox row in the same
transaction as the business change, and a worker drains it. If the process dies
between the two, the row is still there; if the chain call is retried, the
``dedupe_key`` unique index makes enqueueing idempotent.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from app.core.clock import now
from app.db.base import Base
from app.db.models.enums import (
    CHAIN_TX_STATUS,
    ChainTxStatus,
)
from app.db.models.mixins import UUIDPrimaryKeyMixin

__all__ = [
    "ChainEvent",
    "ChainNonce",
    "ChainTx",
    "IndexerCheckpoint",
    "MerkleBatch",
    "MerkleLeaf",
]


class ChainTx(Base, UUIDPrimaryKeyMixin):
    """One broadcast transaction and its confirmation state.

    An outbox row can produce several of these -- a gas bump or a reorg replay
    is a new transaction for the same job -- so this is many-to-one and the
    outbox row is never cascaded away beneath its own audit trail.
    """

    __tablename__ = "chain_txs"

    # Null for a maintenance transaction with no business job behind it -- the
    # zero-value self-send that fills a stranded nonce. Those still have to be
    # recorded: without a row, the gap detector re-finds the same hole on every
    # sweep and pays to fill it again.
    outbox_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("outbox.id", ondelete="RESTRICT"), nullable=True
    )
    tx_hash: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    nonce: Mapped[int] = mapped_column(Integer, nullable=False)
    block_number: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Recorded when the receipt arrives and re-read on every confirmation sweep.
    # Reorg detection is exactly "is the hash at this height still the hash we
    # saw", so it needs the hash we saw, in a column rather than buried in the
    # receipt blob: the comparison that decides whether a record is still true
    # should not depend on the shape of a JSON payload.
    block_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The fees this attempt was signed with, in wei. Replace-by-fee has to bump
    # the *previous* attempt's fee by a percentage, so the previous attempt's
    # fee has to survive the process that sent it. 100 gwei is 1e11, so a
    # bigint is three orders of magnitude clear of any plausible cap.
    max_fee_per_gas: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    max_priority_fee_per_gas: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[ChainTxStatus] = mapped_column(
        CHAIN_TX_STATUS, nullable=False, default=ChainTxStatus.SENT
    )
    gas_used: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    raw_receipt: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_chain_txs_outbox_id", "outbox_id"),
        Index("ix_chain_txs_status", "status"),
        Index("ix_chain_txs_block_number", "block_number"),
    )


class ChainNonce(Base):
    """Next nonce per signing address, one row each.

    Read with ``SELECT ... FOR UPDATE``. Two workers allocating the same nonce
    means one transaction silently replaces the other on chain, so allocation
    is serialised through this row rather than read from the node, whose
    pending count is not authoritative under concurrency.
    """

    __tablename__ = "chain_nonce"

    address: Mapped[str] = mapped_column(Text, primary_key=True)
    next_nonce: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=now,
        onupdate=now,
        server_default=func.now(),
    )


class MerkleBatch(Base, UUIDPrimaryKeyMixin):
    """A batch of item hashes anchored as a single Merkle root.

    One transaction per item does not fit a free-tier gas budget. Batching
    anchors thousands of items for the cost of one write, and each item still
    gets an independently verifiable inclusion proof.
    """

    __tablename__ = "merkle_batches"

    root: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    leaf_count: Mapped[int] = mapped_column(Integer, nullable=False)
    anchored_tx_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chain_txs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now, server_default=func.now()
    )

    __table_args__ = (Index("ix_merkle_batches_anchored_tx_id", "anchored_tx_id"),)


class MerkleLeaf(Base):
    """One item's position in a batch. ``leaf_index`` fixes the proof path."""

    __tablename__ = "merkle_leaves"

    batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("merkle_batches.id", ondelete="CASCADE"),
        primary_key=True,
    )
    leaf_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )
    leaf_hash: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("batch_id", "item_id", name="uq_merkle_leaves_batch_item"),
        Index("ix_merkle_leaves_item_id", "item_id"),
    )


class ChainEvent(Base, UUIDPrimaryKeyMixin):
    """One event read back off the chain, mirrored into Postgres.

    This table is the *observed* side of the system: what the chain says
    happened, as opposed to ``chain_txs``, which is what this system tried to
    make happen. Keeping them separate is what makes reconciliation meaningful
    -- a drift between the two is a real finding, and it would be invisible if
    the writer's own record doubled as the chain's.

    Rebuilt from scratch by ``scripts/replay_chain.py`` using nothing but chain
    events, which is how replayability gets proved rather than asserted.

    ``(tx_hash, log_index)`` is the natural key of a log on any EVM chain, so
    replaying a block range upserts rather than duplicates.
    """

    __tablename__ = "chain_events"

    event_name: Mapped[str] = mapped_column(Text, nullable=False)
    tx_hash: Mapped[str] = mapped_column(Text, nullable=False)
    log_index: Mapped[int] = mapped_column(Integer, nullable=False)
    block_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Recorded so a reorg is detectable in the event mirror too, not only in
    # chain_txs: the same height carrying a different hash means these logs
    # describe a chain that no longer exists.
    block_hash: Mapped[str] = mapped_column(Text, nullable=False)
    contract_address: Mapped[str] = mapped_column(Text, nullable=False)
    # The indexed value the event is about: an item hash, or a Merkle root.
    subject_hash: Mapped[str] = mapped_column(Text, nullable=False)
    issuer_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    issuer_address: Mapped[str] = mapped_column(Text, nullable=False)
    leaf_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The block's timestamp as the contract saw it, not when this row was written.
    chain_timestamp: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("tx_hash", "log_index", name="uq_chain_events_tx_log"),
        Index("ix_chain_events_subject_hash", "subject_hash"),
        Index("ix_chain_events_block_number", "block_number"),
        Index("ix_chain_events_event_name", "event_name"),
    )


class IndexerCheckpoint(Base):
    """Last block each indexer has processed, so restarts resume rather than replay."""

    __tablename__ = "indexer_checkpoints"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    last_block: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=now,
        onupdate=now,
        server_default=func.now(),
    )
