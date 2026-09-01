"""Merkle batching: many item hashes, one root, one transaction.

**The arithmetic this answers.** One ``anchorItem`` transaction per saree costs
roughly 50,000 gas. A lakh of sarees is five billion gas, which on any chain
that charges real money is not a rounding error. Batching a thousand items into
one root costs one transaction -- about 50,000 gas total, roughly a thousandth
per item -- and every item still gets an inclusion proof that verifies
independently against the root the chain holds. The per-item cost falls by about
three orders of magnitude and nothing about verification gets weaker.

**Verification for a batched item.** Recompute the item hash from the Postgres
row exactly as an unbatched item's is recomputed, then check the inclusion proof
against the root, then check that the root is the one on chain. Three steps
instead of two, all of them checkable by anyone holding the row and the chain.

**Leaves are the item hashes, unwrapped.** ``app.core.merkle`` warns that callers
own domain separation, so: every leaf here is a keccak256 digest of a structured
item preimage, and producing a leaf that collides with an internal node would
require a keccak256 preimage attack, not a choice of inputs. Leaves are never
attacker-chosen. If that ever stops being true -- if a leaf could be supplied
directly rather than derived -- the fix is prefixed or double-hashed leaves, and
it would change every root, so it belongs at a version boundary rather than in a
patch.

**Not on the demo path.** ``BATCHING_ENABLED`` defaults to false, so the demo
anchors items one at a time and each registration has its own visible
transaction. This exists tested and off, because "what does this cost at scale"
deserves a running answer rather than a slide.

**The artisan-facing path is gasless.** A weaver should not hold MATIC to
register a saree. ERC-4337 account abstraction with a paymaster lets the
co-operative or the platform sponsor the gas while the weaver still signs their
own claim, which keeps authorship where it belongs. Not built here -- it is a
bundler, a paymaster and a funding policy, none of which fit a free tier -- but
it is the direction, and batching is what makes sponsoring it affordable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chain.outbox import enqueue_job
from app.config import Settings, get_settings
from app.core.hashing import from_hex, hash_hex
from app.core.logging import get_logger
from app.core.merkle import build_proof, build_root, verify_proof
from app.db.models.catalog import Item
from app.db.models.chain import ChainTx, MerkleBatch, MerkleLeaf
from app.db.models.enums import ItemStatus, OutboxJobType, OutboxStatus
from app.db.models.outbox import Outbox

__all__ = [
    "BatchAssembly",
    "InclusionProof",
    "assemble_batch",
    "get_inclusion_proof",
    "verify_inclusion",
]

logger = get_logger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]


@dataclass(frozen=True, slots=True)
class BatchAssembly:
    """A batch that was built and queued for anchoring."""

    batch_id: uuid.UUID
    root: str
    leaf_count: int
    item_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True, slots=True)
class InclusionProof:
    """Everything needed to prove one item sits under an anchored root."""

    item_id: uuid.UUID
    item_hash: str
    root: str
    leaf_index: int
    proof: tuple[str, ...]
    leaf_count: int
    tx_hash: str | None
    block_number: int | None

    @property
    def anchored(self) -> bool:
        """True once the root's transaction is known. Not a confirmation claim."""
        return self.tx_hash is not None


async def assemble_batch(
    session_factory: SessionFactory,
    settings: Settings | None = None,
    limit: int | None = None,
) -> BatchAssembly | None:
    """Fold queued item anchors into one root and queue a single batch anchor.

    The item jobs are completed with a note recording which root absorbed them.
    That is honest -- their anchoring really has been delegated -- and the items
    themselves stay ``PENDING`` until the batch transaction confirms, so nothing
    is claimed on the item's behalf before the chain says so.

    The trade this makes: if the batch job later dies, the item jobs are already
    ``DONE`` and will not retry on their own. ``reconcile`` reports those items
    as in-db-not-on-chain, and ``OutboxRepository.requeue_for_hash`` revives
    them. That is a deliberate, visible failure rather than a silent one, and it
    is part of why batching is off by default.
    """
    resolved = settings or get_settings()
    size = limit or resolved.merkle_batch_size

    async with session_factory() as session:
        jobs = list(
            (
                await session.execute(
                    select(Outbox)
                    .where(
                        Outbox.job_type == OutboxJobType.ANCHOR_ITEM,
                        Outbox.status == OutboxStatus.QUEUED,
                    )
                    .order_by(Outbox.created_at, Outbox.id)
                    .limit(size)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )

        if not jobs:
            return None

        leaves: list[bytes] = []
        item_ids: list[uuid.UUID] = []
        hashes: list[str] = []
        for job in jobs:
            item_hash = str(job.payload["item_hash"])
            item_id = uuid.UUID(str(job.payload["item_id"]))
            leaf = from_hex(item_hash)
            if len(leaf) != 32:
                logger.warning(
                    "chain.batching.bad_leaf",
                    job_id=str(job.id),
                    item_hash=item_hash,
                    action="skipped; a malformed leaf would corrupt the whole root",
                )
                continue
            leaves.append(leaf)
            item_ids.append(item_id)
            hashes.append(item_hash)

        if not leaves:
            await session.commit()
            return None

        root = hash_hex(build_root(leaves))

        existing = (
            await session.execute(select(MerkleBatch).where(MerkleBatch.root == root))
        ).scalar_one_or_none()
        if existing is not None:
            # The same leaf set produces the same root, so this is a replay of a
            # batch that was already assembled. Re-anchoring it would pay twice
            # for one root, and the contract would revert the second attempt.
            await session.commit()
            logger.info("chain.batching.duplicate_root", root=root, batch_id=str(existing.id))
            return BatchAssembly(
                batch_id=existing.id,
                root=root,
                leaf_count=existing.leaf_count,
                item_ids=tuple(item_ids),
            )

        batch = MerkleBatch(root=root, leaf_count=len(leaves))
        session.add(batch)
        await session.flush()

        for index, (item_id, item_hash) in enumerate(zip(item_ids, hashes, strict=True)):
            session.add(
                MerkleLeaf(
                    batch_id=batch.id,
                    leaf_index=index,
                    item_id=item_id,
                    leaf_hash=item_hash,
                )
            )

        await enqueue_job(
            session,
            job_type=OutboxJobType.ANCHOR_BATCH,
            payload={
                "root": root,
                "leaf_count": len(leaves),
                "batch_id": str(batch.id),
            },
            dedupe_key=root,
        )

        for job in jobs:
            job.status = OutboxStatus.DONE
            job.locked_at = None
            job.locked_by = None
            job.last_error = None
            job.payload = {**job.payload, "folded_into_root": root}

        batch_id = batch.id
        await session.commit()

    logger.info(
        "chain.batching.assembled",
        batch_id=str(batch_id),
        root=root,
        leaf_count=len(leaves),
        per_item_cost="one transaction for the whole batch instead of one each",
    )
    return BatchAssembly(
        batch_id=batch_id, root=root, leaf_count=len(leaves), item_ids=tuple(item_ids)
    )


async def get_inclusion_proof(
    session: AsyncSession, item_id: uuid.UUID
) -> InclusionProof | None:
    """Build the proof for one item, entirely from Postgres.

    No chain call. A proof is a fact about a set of leaves and needs nothing but
    the leaves; the chain is consulted once, separately, to check that the root
    it carries is this root.
    """
    leaf_row = (
        await session.execute(select(MerkleLeaf).where(MerkleLeaf.item_id == item_id))
    ).scalar_one_or_none()
    if leaf_row is None:
        return None

    batch = await session.get(MerkleBatch, leaf_row.batch_id)
    if batch is None:
        return None

    rows = list(
        (
            await session.execute(
                select(MerkleLeaf)
                .where(MerkleLeaf.batch_id == batch.id)
                .order_by(MerkleLeaf.leaf_index)
            )
        )
        .scalars()
        .all()
    )
    leaves = [from_hex(row.leaf_hash) for row in rows]
    proof = build_proof(leaves, leaf_row.leaf_index)

    tx_hash: str | None = None
    block_number: int | None = None
    if batch.anchored_tx_id is not None:
        tx = await session.get(ChainTx, batch.anchored_tx_id)
        if tx is not None:
            tx_hash = tx.tx_hash
            block_number = tx.block_number

    return InclusionProof(
        item_id=item_id,
        item_hash=leaf_row.leaf_hash,
        root=batch.root,
        leaf_index=leaf_row.leaf_index,
        proof=tuple(hash_hex(node) for node in proof),
        leaf_count=batch.leaf_count,
        tx_hash=tx_hash,
        block_number=block_number,
    )


def verify_inclusion(item_hash: str, proof: list[str] | tuple[str, ...], root: str) -> bool:
    """Check a proof off-line. The same computation a verifier anywhere would run."""
    return verify_proof(
        from_hex(item_hash), [from_hex(node) for node in proof], from_hex(root)
    )


async def link_batch_transaction(
    session_factory: SessionFactory, root: str, chain_tx_id: uuid.UUID
) -> None:
    """Record which transaction anchored a root, so proofs can cite it."""
    async with session_factory() as session:
        batch = (
            await session.execute(select(MerkleBatch).where(MerkleBatch.root == root))
        ).scalar_one_or_none()
        if batch is None:
            await session.commit()
            return
        batch.anchored_tx_id = chain_tx_id
        await session.commit()


async def pending_item_count(session: AsyncSession) -> int:
    """How many items are still waiting on the chain. Used for batch sizing."""
    rows = (
        await session.execute(select(Item.id).where(Item.status == ItemStatus.PENDING))
    ).all()
    return len(rows)
