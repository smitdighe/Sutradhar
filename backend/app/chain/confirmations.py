"""Receipt polling, confirmation depth, and reorg demotion.

This module decides when a consumer is told their record is on chain, so the bar
is set here and set high.

**A receipt is not a confirmation.** A receipt means the transaction is in a
block. Blocks get orphaned. Promoting an item to ``CONFIRMED`` the moment a
receipt appears is the single most common way a provenance system ends up
asserting something that is no longer true, and it never throws -- the row just
quietly disagrees with the chain. Promotion waits for
``current_block - block_number >= CHAIN_CONFIRMATIONS``.

**A reorg is checked for, not hoped against.** Every sweep re-reads the block at
each shallow transaction's recorded height and compares the hash. A different
hash means the block that carried the anchor no longer exists: the transaction
is ``ORPHANED``, the item goes back to ``PENDING``, the job is requeued, and a
``REORGED`` event records that this happened. The alternative -- assuming a
mined transaction stays mined -- is a record that silently became false.

**A reverted transaction is a failure, not a success.** ``receipt.status == 0``
means the transaction was mined, burned its gas, and did nothing. It has a
receipt and a block number and looks identical to success at a glance.

The one exception is a revert with ``AlreadyAnchored``: the chain is saying the
hash is already recorded, which is what a reorg replay hits when the original
transaction was re-included. That is a completed job, and the item's confirmed
state is then established by the indexer from the real ``ItemAnchored`` event --
not fabricated here, because this attempt has no honest block number to claim.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chain.client import ChainClient, ChainUnavailable, ContractRevert, RpcError, TxReceipt
from app.chain.contract import ContractBinding
from app.chain.outbox import OutboxRepository
from app.chain.writer import ChainWriter
from app.config import Settings, get_settings
from app.core.clock import now, to_rfc3339
from app.core.hashing import hash_object
from app.core.logging import get_logger
from app.db.models.catalog import Item, ItemEvent
from app.db.models.chain import ChainEvent, ChainTx, MerkleBatch, MerkleLeaf
from app.db.models.enums import (
    ChainTxStatus,
    ItemEventType,
    ItemStatus,
    OutboxJobType,
    OutboxStatus,
)
from app.db.models.outbox import Outbox

__all__ = ["ConfirmationSweep", "SweepReport", "promote_from_indexed_event"]

logger = get_logger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]

# How far past the promotion threshold a transaction stays under reorg watch.
# Promotion at CHAIN_CONFIRMATIONS is a judgement that a reorg that deep is
# improbable, not that it is impossible, so the watch continues for the same
# distance again before the transaction is treated as settled. On Polygon, where
# small reorgs are routine, that margin is worth its handful of RPC calls.
REORG_WATCH_MULTIPLIER = 2


@dataclass(slots=True)
class SweepReport:
    """What one confirmation sweep did. Returned for logging and for tests."""

    head_block: int = 0
    receipts_found: int = 0
    promoted: int = 0
    orphaned: int = 0
    reverted: int = 0
    replaced: int = 0
    superseded: int = 0
    errors: list[str] = field(default_factory=list)

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "head_block": self.head_block,
            "receipts_found": self.receipts_found,
            "promoted": self.promoted,
            "orphaned": self.orphaned,
            "reverted": self.reverted,
            "replaced": self.replaced,
            "superseded": self.superseded,
            "errors": len(self.errors),
        }


async def promote_from_indexed_event(
    session: AsyncSession,
    item: Item,
    head_block: int,
    required_depth: int,
    contract_address: str,
    chain_id: int,
) -> bool:
    """Confirm an item from an observed ``ItemAnchored`` event, if one is deep enough.

    The second, narrower promotion path. It exists for the case where the anchor
    on chain was not written by the transaction this system is currently
    tracking: a reorg replay whose original was re-included, or an anchor written
    by a previous deployment against the same contract. The evidence is the
    event itself, with its own block number and its own depth, so nothing is
    invented -- and an item is never promoted from an event shallower than the
    same threshold every other promotion has to clear.

    Caller commits.
    """
    event = (
        await session.execute(
            select(ChainEvent)
            .where(
                ChainEvent.event_name == "ItemAnchored",
                ChainEvent.subject_hash == item.item_hash,
                ChainEvent.contract_address == contract_address,
            )
            .order_by(ChainEvent.block_number)
            .limit(1)
        )
    ).scalar_one_or_none()

    if event is None:
        return False

    depth = head_block - event.block_number
    if depth < required_depth:
        return False

    if item.status == ItemStatus.CONFIRMED:
        return True

    item.status = ItemStatus.CONFIRMED
    session.add(
        ItemEvent(
            item_id=item.id,
            event_type=ItemEventType.ANCHORED,
            actor_id=None,
            payload={
                "tx_hash": event.tx_hash,
                "block_number": event.block_number,
                "block_hash": event.block_hash,
                "confirmations": depth,
                "contract": contract_address,
                "chain_id": chain_id,
                "source": "indexed ItemAnchored event",
                "at": to_rfc3339(now()),
            },
            payload_hash=hash_object(
                {
                    "tx_hash": event.tx_hash,
                    "block_number": event.block_number,
                    "block_hash": event.block_hash,
                    "confirmations": depth,
                }
            ),
        )
    )
    logger.info(
        "chain.item.confirmed_from_event",
        item_id=str(item.id),
        tx_hash=event.tx_hash,
        block_number=event.block_number,
        confirmations=depth,
    )
    return True


class ConfirmationSweep:
    """One pass over in-flight transactions: receipts, reorgs, promotions, bumps."""

    def __init__(
        self,
        client: ChainClient,
        binding: ContractBinding,
        outbox: OutboxRepository,
        session_factory: SessionFactory,
        writer: ChainWriter | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._client = client
        self._binding = binding
        self._outbox = outbox
        self._session_factory = session_factory
        self._writer = writer
        self._settings = settings or get_settings()

    async def run(self) -> SweepReport:
        """Run every stage in order. Reorg detection goes first, deliberately.

        Checking for orphaned blocks before promoting means a transaction whose
        block vanished between two sweeps is demoted rather than promoted on the
        strength of a block number that no longer refers to anything.
        """
        report = SweepReport()
        head = await self._client.block_number()
        report.head_block = head

        await self._detect_reorgs(head, report)
        await self._collect_receipts(head, report)
        await self._promote(head, report)
        await self._replace_stuck(report)

        logger.info("chain.confirmations.sweep", **report.as_log_fields())
        return report

    # ------------------------------------------------------------- reorgs

    async def _detect_reorgs(self, head: int, report: SweepReport) -> None:
        """Re-read each shallow transaction's block and compare hashes."""
        watch_depth = self._settings.chain_confirmations * REORG_WATCH_MULTIPLIER
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(ChainTx).where(
                            ChainTx.status.in_(
                                [ChainTxStatus.MINED, ChainTxStatus.CONFIRMED]
                            ),
                            ChainTx.block_number.is_not(None),
                            ChainTx.block_hash.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )

        for row in rows:
            block_number = row.block_number
            block_hash = row.block_hash
            if block_number is None or block_hash is None:
                continue
            if head - block_number > watch_depth:
                # Settled. Watching every anchor forever would grow the sweep's
                # RPC cost without bound for a vanishing probability.
                continue

            current = await self._client.get_block(block_number)
            if current is not None and current.hash == block_hash:
                continue

            observed = current.hash if current is not None else None
            await self._orphan(row.id, block_number, block_hash, observed)
            report.orphaned += 1

    async def _orphan(
        self,
        chain_tx_id: uuid.UUID,
        block_number: int,
        recorded_hash: str,
        observed_hash: str | None,
    ) -> None:
        """Demote one orphaned transaction and everything that depended on it."""
        async with self._session_factory() as session:
            row = await session.get(ChainTx, chain_tx_id)
            if row is None:
                await session.commit()
                return

            row.status = ChainTxStatus.ORPHANED
            row.confirmations = 0
            job = await session.get(Outbox, row.outbox_id) if row.outbox_id else None

            items = await self._items_for_job(session, job)
            for item in items:
                # Back to the honest state. The hash was anchored in a block
                # that no longer exists, so as far as any verifier is concerned
                # it was never anchored at all.
                item.status = ItemStatus.PENDING
                self._add_event(
                    session,
                    item.id,
                    ItemEventType.REORGED,
                    {
                        "reason": "the block carrying this anchor was reorganised out",
                        "tx_hash": row.tx_hash,
                        "block_number": block_number,
                        "recorded_block_hash": recorded_hash,
                        "observed_block_hash": observed_hash,
                        "at": to_rfc3339(now()),
                    },
                )

            if job is not None:
                job.status = OutboxStatus.QUEUED
                job.attempts = 0
                job.locked_at = None
                job.locked_by = None
                job.next_attempt_at = now()

            await session.commit()

        logger.warning(
            "chain.reorg.detected",
            chain_tx_id=str(chain_tx_id),
            block_number=block_number,
            recorded_block_hash=recorded_hash,
            observed_block_hash=observed_hash,
            action="transaction ORPHANED, item(s) demoted to PENDING, job requeued",
        )

    # ------------------------------------------------------------ receipts

    async def _collect_receipts(self, head: int, report: SweepReport) -> None:
        """Look for receipts for everything still marked SENT."""
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(ChainTx).where(
                            ChainTx.status == ChainTxStatus.SENT,
                            ChainTx.tx_hash.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )

        for row in rows:
            if row.tx_hash is None:
                continue
            try:
                receipt = await self._client.get_transaction_receipt(row.tx_hash)
            except ChainUnavailable as exc:
                report.errors.append(f"{row.tx_hash}: {exc.message}")
                continue
            except RpcError as exc:
                report.errors.append(f"{row.tx_hash}: {exc}")
                logger.warning("chain.receipt.error", tx_hash=row.tx_hash, error=str(exc))
                continue

            if receipt is None:
                continue

            report.receipts_found += 1
            if receipt.reverted:
                await self._handle_revert(row.id, receipt)
                report.reverted += 1
            else:
                superseded = await self._record_mined(row.id, receipt, head)
                report.superseded += superseded

    async def _record_mined(self, chain_tx_id: uuid.UUID, receipt: TxReceipt, head: int) -> int:
        """Mark a transaction mined. Items stay PENDING until depth is reached.

        Returns how many same-nonce siblings were retired. Replace-by-fee leaves
        several attempts at one nonce and exactly one of them can mine; the rest
        can never mine and would otherwise be polled for a receipt forever.
        """
        superseded = 0
        async with self._session_factory() as session:
            row = await session.get(ChainTx, chain_tx_id)
            if row is None:
                await session.commit()
                return 0

            row.status = ChainTxStatus.MINED
            row.block_number = receipt.block_number
            row.block_hash = receipt.block_hash
            row.gas_used = receipt.gas_used
            row.confirmations = max(0, head - receipt.block_number)
            row.raw_receipt = {
                "status": receipt.status,
                "blockNumber": receipt.block_number,
                "blockHash": receipt.block_hash,
                "gasUsed": receipt.gas_used,
                "transactionHash": receipt.tx_hash,
            }

            siblings = list(
                (
                    await session.execute(
                        select(ChainTx).where(
                            ChainTx.nonce == row.nonce,
                            ChainTx.id != row.id,
                            ChainTx.status == ChainTxStatus.SENT,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for sibling in siblings:
                sibling.status = ChainTxStatus.FAILED
                sibling.raw_receipt = {"superseded_by": receipt.tx_hash}
                superseded += 1

            await session.commit()

        logger.info(
            "chain.tx.mined",
            tx_hash=receipt.tx_hash,
            block_number=receipt.block_number,
            gas_used=receipt.gas_used,
            superseded=superseded,
            note="item stays PENDING until the confirmation depth is reached",
        )
        return superseded

    async def _handle_revert(self, chain_tx_id: uuid.UUID, receipt: TxReceipt) -> None:
        """A mined transaction that did nothing. Never treated as an anchor."""
        reason = await self._recover_revert_reason(chain_tx_id)

        # "Already anchored" is the one revert that means the work is done.
        if reason is not None and reason.is_already_anchored:
            async with self._session_factory() as session:
                row = await session.get(ChainTx, chain_tx_id)
                if row is not None:
                    row.status = ChainTxStatus.FAILED
                    row.block_number = receipt.block_number
                    row.block_hash = receipt.block_hash
                    row.gas_used = receipt.gas_used
                    row.raw_receipt = {"status": 0, "revert": str(reason)}
                    job_id = row.outbox_id
                else:
                    job_id = None
                await session.commit()

            if job_id is not None:
                # Released, not completed. This attempt has no honest block
                # number to promote the item with -- the real anchor belongs to
                # some earlier transaction. The next drain finds that anchor in
                # the event mirror and promotes from it. If the indexer never
                # produces one, the job eventually dead-letters with an error
                # chain saying the chain reports an anchor nobody indexed, which
                # is an anomaly worth a human's attention, not something to paper over.
                await self._outbox.release(
                    job_id,
                    "chain reports AlreadyAnchored; awaiting the indexed "
                    "ItemAnchored event to confirm from",
                )
            logger.info(
                "chain.tx.reverted_already_anchored",
                tx_hash=receipt.tx_hash,
                resolution="job requeued; confirmation will come from the indexed event, "
                "not from this attempt",
            )
            return

        detail = str(reason) if reason is not None else "revert reason unavailable"
        async with self._session_factory() as session:
            row = await session.get(ChainTx, chain_tx_id)
            if row is None:
                await session.commit()
                return
            row.status = ChainTxStatus.FAILED
            row.block_number = receipt.block_number
            row.block_hash = receipt.block_hash
            row.gas_used = receipt.gas_used
            row.raw_receipt = {"status": 0, "revert": detail}
            job = await session.get(Outbox, row.outbox_id) if row.outbox_id else None
            job_id = job.id if job is not None else None

            for item in await self._items_for_job(session, job):
                item.status = ItemStatus.FAILED
                self._add_event(
                    session,
                    item.id,
                    ItemEventType.ANCHOR_FAILED,
                    {
                        "reason": detail,
                        "tx_hash": row.tx_hash,
                        "block_number": receipt.block_number,
                        "at": to_rfc3339(now()),
                    },
                )
            await session.commit()

        if job_id is not None:
            await self._outbox.kill(job_id, f"transaction reverted on chain: {detail}")

        logger.error(
            "chain.tx.reverted",
            tx_hash=receipt.tx_hash,
            block_number=receipt.block_number,
            gas_used=receipt.gas_used,
            revert=detail,
            action="item(s) marked FAILED and the job dead-lettered",
        )

    async def _recover_revert_reason(self, chain_tx_id: uuid.UUID) -> Any:
        """Best-effort revert reason, by replaying the call at the current head.

        Exact for a deterministic revert -- ``AlreadyAnchored`` reverts the same
        way at any height -- and best-effort otherwise, because the state that
        caused the revert may have moved on. An unrecoverable reason is recorded
        as unavailable rather than guessed at.
        """
        async with self._session_factory() as session:
            row = await session.get(ChainTx, chain_tx_id)
            job = (
                await session.get(Outbox, row.outbox_id)
                if row is not None and row.outbox_id
                else None
            )
            payload = dict(job.payload) if job is not None else None
            job_type = job.job_type if job is not None else None
            await session.commit()

        if payload is None or job_type is None:
            return None

        try:
            if job_type == OutboxJobType.ANCHOR_ITEM:
                calldata = self._binding.encode_anchor_item(
                    str(payload["item_hash"]), str(payload["issuer_hash"])
                )
            elif job_type == OutboxJobType.ANCHOR_ATTESTATION:
                calldata = self._binding.encode_anchor_item(
                    str(payload["statement_hash"]), str(payload["issuer_hash"])
                )
            elif job_type == OutboxJobType.ANCHOR_BATCH:
                calldata = self._binding.encode_anchor_batch(
                    str(payload["root"]), int(str(payload["leaf_count"]))
                )
            else:
                # A job type with no anchoring calldata to replay -- PIN_MEDIA
                # never produces a transaction, so it never produces a revert
                # to recover a reason for.
                return None
        except (KeyError, ValueError) as exc:
            logger.warning("chain.revert.payload_unusable", error=str(exc))
            return None

        try:
            await self._client.call(
                {
                    "to": self._binding.address,
                    "data": "0x" + calldata.hex(),
                    "value": 0,
                }
            )
        except ContractRevert as exc:
            return self._binding.decode_revert(exc.data)
        except (RpcError, ChainUnavailable) as exc:
            logger.info("chain.revert.replay_failed", error=str(exc))
            return None
        # The replay succeeded, so whatever made the original revert is gone.
        return None

    # ------------------------------------------------------------ promotion

    async def _promote(self, head: int, report: SweepReport) -> None:
        """Promote mined transactions that have reached the required depth."""
        required = self._settings.chain_confirmations

        async with self._session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(ChainTx).where(
                            ChainTx.status == ChainTxStatus.MINED,
                            ChainTx.block_number.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )

            promoted = 0
            for row in rows:
                block_number = row.block_number
                if block_number is None:
                    continue
                depth = head - block_number
                row.confirmations = max(0, depth)
                if depth < required:
                    # Not deep enough. The item stays PENDING, which is the
                    # honest answer to "is this on chain yet".
                    continue

                row.status = ChainTxStatus.CONFIRMED
                job = await session.get(Outbox, row.outbox_id) if row.outbox_id else None
                for item in await self._items_for_job(session, job):
                    item.status = ItemStatus.CONFIRMED
                    self._add_event(
                        session,
                        item.id,
                        ItemEventType.ANCHORED,
                        {
                            "tx_hash": row.tx_hash,
                            "block_number": block_number,
                            "block_hash": row.block_hash,
                            "confirmations": depth,
                            "contract": self._binding.address,
                            "chain_id": self._settings.chain_id,
                            "at": to_rfc3339(now()),
                        },
                    )
                if job is not None:
                    job.status = OutboxStatus.DONE
                    job.locked_at = None
                    job.locked_by = None
                promoted += 1

            await session.commit()

        report.promoted = promoted
        if promoted:
            logger.info("chain.confirmations.promoted", count=promoted, required_depth=required)

    # ------------------------------------------------------- replace-by-fee

    async def _replace_stuck(self, report: SweepReport) -> None:
        """Bump the fee on transactions that have sat unmined past the timeout."""
        if self._writer is None or not self._settings.chain_write_enabled:
            return

        cutoff = now() - timedelta(seconds=self._settings.chain_tx_timeout_seconds)
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(ChainTx)
                        .where(
                            ChainTx.status == ChainTxStatus.SENT,
                            ChainTx.created_at < cutoff,
                        )
                        .order_by(ChainTx.nonce)
                    )
                )
                .scalars()
                .all()
            )

        # One replacement per nonce per sweep. Several stale attempts can share
        # a nonce, and bumping each of them would send a burst of mutually
        # replacing transactions in one pass.
        seen: set[int] = set()
        for row in rows:
            if row.nonce in seen:
                continue
            seen.add(row.nonce)

            attempts = await self._attempts_at_nonce(row.nonce)
            if attempts >= self._settings.chain_max_rbf_attempts:
                logger.warning(
                    "chain.tx.rbf_exhausted",
                    nonce=row.nonce,
                    attempts=attempts,
                    consequence="the nonce stays pending; later transactions remain queued "
                    "behind it until it mines or the gap filler steps in",
                )
                continue

            result = await self._writer.replace(row)
            if result.succeeded:
                report.replaced += 1
            else:
                logger.warning(
                    "chain.tx.replace_failed",
                    nonce=row.nonce,
                    outcome=str(result.outcome),
                    reason=result.reason,
                )

    async def _attempts_at_nonce(self, nonce: int) -> int:
        async with self._session_factory() as session:
            rows = (
                await session.execute(select(ChainTx.id).where(ChainTx.nonce == nonce))
            ).all()
            await session.commit()
            return len(rows)

    # --------------------------------------------------------------- shared

    async def _items_for_job(
        self, session: AsyncSession, job: Outbox | None
    ) -> list[Item]:
        """Every item whose anchored state depends on this job.

        One for an item anchor; the whole leaf set for a batch, because a
        reorged batch un-anchors every item it covered.
        """
        if job is None:
            return []

        if job.job_type == OutboxJobType.ANCHOR_ITEM:
            raw_id = job.payload.get("item_id")
            if raw_id is None:
                return []
            item = await session.get(Item, uuid.UUID(str(raw_id)))
            return [item] if item is not None else []

        if job.job_type == OutboxJobType.ANCHOR_BATCH:
            root = job.payload.get("root")
            if root is None:
                return []
            batch = (
                await session.execute(select(MerkleBatch).where(MerkleBatch.root == str(root)))
            ).scalar_one_or_none()
            if batch is None:
                return []
            item_ids = (
                (
                    await session.execute(
                        select(MerkleLeaf.item_id).where(MerkleLeaf.batch_id == batch.id)
                    )
                )
                .scalars()
                .all()
            )
            if not item_ids:
                return []
            return list(
                (await session.execute(select(Item).where(Item.id.in_(item_ids))))
                .scalars()
                .all()
            )

        return []

    @staticmethod
    def _add_event(
        session: AsyncSession,
        item_id: uuid.UUID,
        event_type: ItemEventType,
        payload: dict[str, Any],
    ) -> None:
        """Append a provenance event. Actor is null: the chain did this, not a person."""
        session.add(
            ItemEvent(
                item_id=item_id,
                event_type=event_type,
                actor_id=None,
                payload=payload,
                payload_hash=hash_object(payload),
            )
        )
