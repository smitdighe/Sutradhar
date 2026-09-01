"""Tailing contract events into Postgres so nothing has to read the chain to browse.

**Why this exists at all.** Answering "show me this weaver's anchored items" by
looping ``eth_getLogs`` works for a demo and collapses under real volume: every
page view becomes a paid RPC round trip, latency tracks whatever the provider
feels like today, and a rate limit turns a browse into an outage. Instead the
chain is read once, into Postgres, and the frontend queries Postgres. The chain
is touched at browse time only to verify a single hash on demand.

**The production path is The Graph.** A subgraph does this job with reorg
handling, historical backfill and a hosted query layer already built, and would
replace this module more or less wholesale. It is not built here because it adds
a hosted service and a second deployment to a system whose free-tier constraint
is the whole point. This module is the honest small version of it, and the
reasons it would be replaced are the reasons above, not a defect in it.

Three rules that make replay safe:

**Fixed windows.** Providers cap ``eth_getLogs`` responses, and the cap is
discovered as an error at ten thousand blocks rather than announced. Windows are
sized to stay inside it.

**Checkpoint after commit, never before.** A crash mid-batch has to replay that
window, not skip it. Writing the checkpoint first would turn every crash into a
permanent hole in the index that nothing would ever notice.

**Rescan the confirmation window every run.** Starting from
``last_block - CHAIN_CONFIRMATIONS`` rather than trusting the checkpoint means a
reorg near the head is re-read rather than believed. Within that rescan window
the events are *replaced*, not merged: whatever the chain says now is what the
mirror holds, so an orphaned block's logs do not linger as an assertion about a
chain that no longer exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chain.client import ChainClient, ChainUnavailable, LogEntry, RpcError
from app.chain.contract import BatchAnchoredEvent, ContractBinding, ItemAnchoredEvent
from app.config import Settings, get_settings
from app.core.clock import now
from app.core.ids import new_uuid
from app.core.logging import get_logger
from app.db.models.chain import ChainEvent, IndexerCheckpoint

__all__ = ["INDEXER_NAME", "EventIndexer", "IndexReport"]

logger = get_logger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]

INDEXER_NAME = "sutradhar_anchors"


@dataclass(slots=True)
class IndexReport:
    """What one indexer pass covered and found."""

    from_block: int = 0
    to_block: int = 0
    head_block: int = 0
    windows: int = 0
    logs_seen: int = 0
    events_written: int = 0
    events_removed: int = 0
    unknown_logs: int = 0
    errors: list[str] = field(default_factory=list)

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "from_block": self.from_block,
            "to_block": self.to_block,
            "head_block": self.head_block,
            "windows": self.windows,
            "logs_seen": self.logs_seen,
            "events_written": self.events_written,
            "events_removed": self.events_removed,
            "unknown_logs": self.unknown_logs,
            "errors": len(self.errors),
        }


class EventIndexer:
    """Reads ``ItemAnchored`` and ``BatchAnchored`` into ``chain_events``."""

    def __init__(
        self,
        client: ChainClient,
        binding: ContractBinding,
        session_factory: SessionFactory,
        settings: Settings | None = None,
        name: str = INDEXER_NAME,
    ) -> None:
        self._client = client
        self._binding = binding
        self._session_factory = session_factory
        self._settings = settings or get_settings()
        self._name = name

    async def run(self, until_block: int | None = None) -> IndexReport:
        """Index from the checkpoint to the head, one fixed window at a time."""
        report = IndexReport()

        try:
            head = until_block if until_block is not None else await self._client.block_number()
        except (RpcError, ChainUnavailable) as exc:
            report.errors.append(str(exc))
            logger.warning("chain.indexer.head_unavailable", error=str(exc))
            return report

        report.head_block = head
        checkpoint = await self._read_checkpoint()
        # Rescan the confirmation window: anything shallower than this may still
        # be reorganised, and re-reading it is cheaper than being wrong about it.
        start = max(0, checkpoint - self._settings.chain_confirmations)
        report.from_block = start

        if start > head:
            report.to_block = head
            return report

        window = max(1, self._settings.indexer_block_range)
        cursor = start
        while cursor <= head:
            upper = min(cursor + window - 1, head)
            try:
                logs = await self._fetch_window(cursor, upper)
            except (RpcError, ChainUnavailable) as exc:
                # Stop here rather than skipping ahead. The checkpoint stays
                # where it was, so the next run resumes from this window instead
                # of leaving a hole nothing would ever revisit.
                report.errors.append(f"blocks {cursor}-{upper}: {exc}")
                logger.warning(
                    "chain.indexer.window_failed",
                    from_block=cursor,
                    to_block=upper,
                    error=str(exc),
                    action="checkpoint held; this window is retried on the next run",
                )
                break

            report.windows += 1
            report.logs_seen += len(logs)
            written, removed, unknown = await self._persist_window(cursor, upper, logs)
            report.events_written += written
            report.events_removed += removed
            report.unknown_logs += unknown

            # Committed above, so the checkpoint can now safely move.
            await self._write_checkpoint(upper)
            report.to_block = upper
            cursor = upper + 1

        logger.info("chain.indexer.pass", **report.as_log_fields())
        return report

    # ---------------------------------------------------------------- steps

    async def _fetch_window(self, from_block: int, to_block: int) -> list[LogEntry]:
        """Both anchoring topics in one request.

        A ``topics`` array whose first element is a list means "topic0 is any of
        these", so one ``eth_getLogs`` covers both events instead of two.
        """
        topics: list[Any] = [
            [self._binding.item_anchored_topic, self._binding.batch_anchored_topic]
        ]
        return await self._client.get_logs(
            self._binding.address, topics, from_block, to_block
        )

    async def _persist_window(
        self, from_block: int, to_block: int, logs: list[LogEntry]
    ) -> tuple[int, int, int]:
        """Replace this window's events with what the chain just returned.

        Delete-then-insert inside one transaction, rather than a plain upsert.
        An upsert alone would leave the logs of a reorganised-away block in the
        mirror forever, still asserting that something happened in a block that
        no longer exists. Re-reading the window from the chain makes the chain's
        current answer the only answer.

        The ``(tx_hash, log_index)`` unique index is still honoured on insert, so
        a window processed twice converges rather than duplicating.
        """
        decoded: list[ItemAnchoredEvent | BatchAnchoredEvent] = []
        unknown = 0
        for entry in logs:
            parsed = self._binding.decode_log(entry)
            if parsed is None:
                unknown += 1
                continue
            decoded.append(parsed)

        async with self._session_factory() as session:
            removed = await self._clear_window(session, from_block, to_block)
            for event in decoded:
                await session.execute(self._upsert(event))
            await session.commit()

        if unknown:
            logger.debug(
                "chain.indexer.unknown_logs",
                count=unknown,
                from_block=from_block,
                to_block=to_block,
                note="topics this version does not decode; skipped, not fatal",
            )
        return len(decoded), removed, unknown

    async def _clear_window(
        self, session: AsyncSession, from_block: int, to_block: int
    ) -> int:
        result = await session.execute(
            delete(ChainEvent).where(
                ChainEvent.block_number >= from_block,
                ChainEvent.block_number <= to_block,
                ChainEvent.contract_address == self._binding.address,
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)

    def _upsert(self, event: ItemAnchoredEvent | BatchAnchoredEvent) -> Any:
        """One ``INSERT ... ON CONFLICT`` keyed on the log's natural identity."""
        if isinstance(event, ItemAnchoredEvent):
            values: dict[str, Any] = {
                "event_name": "ItemAnchored",
                "subject_hash": event.item_hash,
                "issuer_hash": event.issuer_hash,
                "leaf_count": None,
                "payload": {
                    "itemHash": event.item_hash,
                    "issuerHash": event.issuer_hash,
                    "issuer": event.issuer,
                    "timestamp": event.timestamp,
                },
            }
        else:
            values = {
                "event_name": "BatchAnchored",
                "subject_hash": event.root,
                "issuer_hash": None,
                "leaf_count": event.leaf_count,
                "payload": {
                    "root": event.root,
                    "leafCount": event.leaf_count,
                    "issuer": event.issuer,
                    "timestamp": event.timestamp,
                },
            }

        values.update(
            {
                "id": new_uuid(),
                "tx_hash": event.tx_hash,
                "log_index": event.log_index,
                "block_number": event.block_number,
                "block_hash": event.block_hash,
                "contract_address": self._binding.address,
                "issuer_address": event.issuer,
                "chain_timestamp": event.timestamp,
                "observed_at": now(),
            }
        )

        updatable = {
            key: values[key]
            for key in (
                "event_name",
                "block_number",
                "block_hash",
                "subject_hash",
                "issuer_hash",
                "issuer_address",
                "leaf_count",
                "chain_timestamp",
                "payload",
                "observed_at",
            )
        }
        return (
            insert(ChainEvent)
            .values(**values)
            .on_conflict_do_update(constraint="uq_chain_events_tx_log", set_=updatable)
        )

    # ----------------------------------------------------------- checkpoint

    async def _read_checkpoint(self) -> int:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(IndexerCheckpoint).where(IndexerCheckpoint.name == self._name)
                )
            ).scalar_one_or_none()
            await session.commit()
            return row.last_block if row is not None else 0

    async def _write_checkpoint(self, block_number: int) -> None:
        """Advance the checkpoint. Only ever called after a committed window."""
        async with self._session_factory() as session:
            await session.execute(
                insert(IndexerCheckpoint)
                .values(name=self._name, last_block=block_number, updated_at=now())
                .on_conflict_do_update(
                    index_elements=[IndexerCheckpoint.name],
                    set_={"last_block": block_number, "updated_at": now()},
                )
            )
            await session.commit()

    async def reset_checkpoint(self, block_number: int = 0) -> None:
        """Rewind the indexer. Used by ``scripts/replay_chain.py``."""
        await self._write_checkpoint(block_number)
        logger.warning(
            "chain.indexer.checkpoint_reset",
            name=self._name,
            last_block=block_number,
            consequence="the next run re-reads the chain from this height",
        )
