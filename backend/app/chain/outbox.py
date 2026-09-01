"""The transactional outbox: claiming, retrying, and parking anchoring jobs.

There is no message broker here and there does not need to be one. Postgres has
the two primitives a queue actually requires -- row locks and ``SKIP LOCKED`` --
and using them means a job and the business change that produced it commit in
the *same transaction*. A broker cannot offer that: publishing to Redis and
committing to Postgres are two writes, and every ordering of those two has a
crash window that either loses the job or invents one.

``FOR UPDATE SKIP LOCKED`` is what makes concurrent workers safe. Each claim
takes row locks on the candidates it selects and steps over rows another worker
already holds, so two workers draining at once split the queue instead of
fighting over its head.

The failure paths matter more than the happy one:

**A crashed worker must not strand its jobs.** A claim is a lease, not a
transfer: ``locked_at`` is stamped on claim and any ``IN_FLIGHT`` row older than
``OUTBOX_LOCK_STALE_SECONDS`` goes back to ``QUEUED``. That threshold must stay
comfortably above ``CHAIN_TX_TIMEOUT_SECONDS`` or a slow-but-alive send gets its
row stolen and the same anchor is sent twice.

**Nothing is dropped silently.** After ``OUTBOX_MAX_ATTEMPTS`` a job is marked
``DEAD`` and copied into ``dead_letters`` with every attempt's error, not just
the last one. No handler in this package catches an exception and does nothing
with it -- bare handlers and no-op bodies are both banned, and a test reads the
source to enforce it. A swallowed exception in a queue drain is
indistinguishable from a queue that works: the jobs stop, nothing is logged, and
every symptom points at the chain.

**Enqueueing twice must not anchor twice.** ``dedupe_key`` is uniquely indexed,
so a re-registration collides at the index instead of producing a second
transaction and a second gas bill.
"""

from __future__ import annotations

import os
import random
import socket
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.core.clock import now, to_rfc3339
from app.core.logging import get_logger
from app.db.models.enums import OutboxJobType, OutboxStatus
from app.db.models.ops import DeadLetter
from app.db.models.outbox import Outbox

__all__ = [
    "ClaimedJob",
    "OutboxRepository",
    "enqueue_job",
    "worker_identity",
]

logger = get_logger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]

DEAD_LETTER_SOURCE = "chain_outbox"


def worker_identity() -> str:
    """Stable-enough identity for a claim lease: host and process."""
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """A claimed outbox row, detached from the session that claimed it.

    A plain snapshot rather than an ORM instance: the job is handed to a worker
    that will open several short transactions while sending, and a live instance
    bound to a closed session is a lazy-load exception waiting for the least
    convenient moment.
    """

    id: uuid.UUID
    job_type: OutboxJobType
    payload: dict[str, Any]
    dedupe_key: str
    attempts: int
    error_chain: list[dict[str, Any]] = field(default_factory=list)


async def enqueue_job(
    session: AsyncSession,
    *,
    job_type: OutboxJobType,
    payload: dict[str, Any],
    dedupe_key: str,
) -> bool:
    """Enqueue idempotently. Returns whether a new row was created.

    Uses ``ON CONFLICT DO NOTHING`` rather than a select-then-insert: the check
    and the insert would be two statements with a race between them, and losing
    that race means two transactions anchoring the same hash.

    Does **not** commit. The caller commits, in the same transaction as whatever
    business change made the job necessary -- that is the entire point of an
    outbox.
    """
    result = await session.execute(
        insert(Outbox)
        .values(
            job_type=job_type,
            payload=payload,
            dedupe_key=dedupe_key,
            status=OutboxStatus.QUEUED,
            attempts=0,
            error_chain=[],
        )
        .on_conflict_do_nothing(index_elements=[Outbox.dedupe_key])
        .returning(Outbox.id)
    )
    return result.scalar_one_or_none() is not None


class OutboxRepository:
    """Claim, complete, retry and park outbox jobs."""

    def __init__(
        self,
        session_factory: SessionFactory,
        settings: Settings | None = None,
        worker_id: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings or get_settings()
        self.worker_id = worker_id or worker_identity()

    # ---------------------------------------------------------------- claim

    async def claim(
        self,
        limit: int | None = None,
        job_types: Sequence[OutboxJobType] | None = None,
    ) -> list[ClaimedJob]:
        """Lease up to *limit* due jobs, oldest first.

        The subquery takes the row locks and skips whatever another worker
        already holds; the outer ``UPDATE`` stamps the lease. One statement, so
        there is no window in which a row is selected but not yet claimed.

        *job_types* is not an optimisation. One table now carries work for
        several drains, and a drain that claims a job it cannot run does real
        damage: the chain drain would take a pin job, fail to dispatch it, and
        kill it as unsupported -- or, worse, release it in a loop because chain
        writes happen to be disabled. Each drain filters to the types it
        understands, so a job is only ever leased by something that can run it.
        """
        batch = limit or self._settings.outbox_batch_size
        moment = now()

        conditions = [
            Outbox.status == OutboxStatus.QUEUED,
            Outbox.next_attempt_at <= moment,
        ]
        if job_types is not None:
            conditions.append(Outbox.job_type.in_(list(job_types)))

        candidates = (
            select(Outbox.id)
            .where(*conditions)
            # Creation order, so an item registered first is anchored first.
            # Nothing depends on it for correctness, but a queue that reorders
            # itself is much harder to reason about when it misbehaves.
            .order_by(Outbox.created_at, Outbox.id)
            .limit(batch)
            .with_for_update(skip_locked=True)
        )

        statement = (
            update(Outbox)
            .where(Outbox.id.in_(candidates.scalar_subquery()))
            .values(status=OutboxStatus.IN_FLIGHT, locked_at=moment, locked_by=self.worker_id)
            .returning(Outbox)
        )

        async with self._session_factory() as session:
            rows = (await session.execute(statement)).scalars().all()
            # The ORDER BY in the subquery decides *which* rows are claimed; it
            # does not survive into RETURNING, whose row order is unspecified.
            # Re-sorting here is what actually makes the batch drain oldest
            # first, so an item registered before another is anchored before it.
            rows = sorted(rows, key=lambda row: (row.created_at, row.id))
            claimed = [
                ClaimedJob(
                    id=row.id,
                    job_type=row.job_type,
                    payload=dict(row.payload),
                    dedupe_key=row.dedupe_key,
                    attempts=row.attempts,
                    error_chain=list(row.error_chain or []),
                )
                for row in rows
            ]
            await session.commit()

        if claimed:
            logger.debug("outbox.claimed", count=len(claimed), worker=self.worker_id)
        return claimed

    async def reclaim_stale(self, job_types: Sequence[OutboxJobType] | None = None) -> int:
        """Return leases held longer than the threshold to ``QUEUED``.

        A worker killed mid-send leaves its rows ``IN_FLIGHT`` forever. Without
        this the queue drains to a halt one crash at a time, and every symptom
        points at the chain rather than at the worker.
        """
        cutoff = now() - timedelta(seconds=self._settings.outbox_lock_stale_seconds)
        conditions = [
            Outbox.status == OutboxStatus.IN_FLIGHT,
            Outbox.locked_at.is_not(None),
            Outbox.locked_at < cutoff,
        ]
        if job_types is not None:
            conditions.append(Outbox.job_type.in_(list(job_types)))

        statement = (
            update(Outbox)
            .where(*conditions)
            .values(status=OutboxStatus.QUEUED, locked_at=None, locked_by=None)
            .returning(Outbox.id, Outbox.locked_by)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
            await session.commit()

        if rows:
            logger.warning(
                "outbox.stale_locks_reclaimed",
                count=len(rows),
                stale_after_seconds=self._settings.outbox_lock_stale_seconds,
                previous_holders=sorted({str(row[1]) for row in rows}),
            )
        return len(rows)

    # ------------------------------------------------------------ outcomes

    async def complete(self, job_id: uuid.UUID, detail: str = "") -> None:
        """Mark a job done. Terminal; nothing reopens it."""
        async with self._session_factory() as session:
            row = await session.get(Outbox, job_id)
            if row is None:
                logger.warning("outbox.complete.missing", job_id=str(job_id))
                await session.commit()
                return
            row.status = OutboxStatus.DONE
            row.locked_at = None
            row.locked_by = None
            row.last_error = None
            await session.commit()
        logger.info("outbox.completed", job_id=str(job_id), detail=detail or None)

    async def release(self, job_id: uuid.UUID, reason: str) -> None:
        """Requeue without counting an attempt.

        For refusals that are about *this system's* configuration rather than
        the job: writes disabled, no signer, fee above the cap, budget spent.
        Counting those toward the retry limit would dead-letter perfectly good
        jobs for the duration of an outage and lose them when it ended.
        """
        async with self._session_factory() as session:
            row = await session.get(Outbox, job_id)
            if row is None:
                await session.commit()
                return
            row.status = OutboxStatus.QUEUED
            row.locked_at = None
            row.locked_by = None
            row.last_error = reason
            row.next_attempt_at = self._backoff_from(row.attempts)
            await session.commit()
        logger.info("outbox.released", job_id=str(job_id), reason=reason)

    async def fail(self, job_id: uuid.UUID, error: str) -> OutboxStatus:
        """Record a real failure: back off, or park after the last attempt."""
        async with self._session_factory() as session:
            row = await session.get(Outbox, job_id)
            if row is None:
                logger.warning("outbox.fail.missing", job_id=str(job_id))
                await session.commit()
                return OutboxStatus.DEAD

            row.attempts += 1
            row.last_error = error
            # Reassigned, never appended to: JSONB has no in-place change
            # tracking and a mutation would be dropped at flush.
            row.error_chain = [
                *(row.error_chain or []),
                {"attempt": row.attempts, "at": to_rfc3339(now()), "error": error},
            ]
            row.locked_at = None
            row.locked_by = None

            if row.attempts >= self._settings.outbox_max_attempts:
                row.status = OutboxStatus.DEAD
                session.add(
                    DeadLetter(
                        source=DEAD_LETTER_SOURCE,
                        original_payload={
                            "job_id": str(row.id),
                            "job_type": str(row.job_type),
                            "dedupe_key": row.dedupe_key,
                            "payload": dict(row.payload),
                        },
                        error_chain=_render_chain(row.error_chain),
                        attempts=row.attempts,
                    )
                )
                await session.commit()
                logger.error(
                    "outbox.dead_lettered",
                    job_id=str(job_id),
                    attempts=row.attempts,
                    last_error=error,
                    action="parked in dead_letters with the full error history",
                )
                return OutboxStatus.DEAD

            row.status = OutboxStatus.QUEUED
            row.next_attempt_at = self._backoff_from(row.attempts)
            # Read out before the commit: an expired instance re-fetches on
            # attribute access, and the session is about to close.
            attempt_number, retry_at = row.attempts, row.next_attempt_at
            await session.commit()

        logger.warning(
            "outbox.retry_scheduled",
            job_id=str(job_id),
            attempt=attempt_number,
            next_attempt_at=retry_at.isoformat(),
            error=error,
        )
        return OutboxStatus.QUEUED

    async def kill(self, job_id: uuid.UUID, error: str) -> None:
        """Park a job immediately, without spending its remaining attempts.

        For failures that retrying cannot fix: a transaction that reverted on
        chain will revert again against the same state. Burning five more
        attempts and five more gas payments to reach the same dead letter would
        cost real money to learn nothing.
        """
        async with self._session_factory() as session:
            row = await session.get(Outbox, job_id)
            if row is None:
                await session.commit()
                return
            row.attempts += 1
            row.last_error = error
            row.error_chain = [
                *(row.error_chain or []),
                {
                    "attempt": row.attempts,
                    "at": to_rfc3339(now()),
                    "error": error,
                    "terminal": True,
                },
            ]
            row.status = OutboxStatus.DEAD
            row.locked_at = None
            row.locked_by = None
            session.add(
                DeadLetter(
                    source=DEAD_LETTER_SOURCE,
                    original_payload={
                        "job_id": str(row.id),
                        "job_type": str(row.job_type),
                        "dedupe_key": row.dedupe_key,
                        "payload": dict(row.payload),
                    },
                    error_chain=_render_chain(row.error_chain),
                    attempts=row.attempts,
                )
            )
            attempts = row.attempts
            await session.commit()

        logger.error(
            "outbox.killed",
            job_id=str(job_id),
            attempts=attempts,
            error=error,
            reason="terminal failure; retrying cannot change the outcome",
        )

    async def requeue_dead(self, job_id: uuid.UUID) -> bool:
        """Put a parked job back on the queue, attempts reset.

        Used by reorg handling, where the job did not fail -- the chain changed
        its mind -- and by an operator resolving a dead letter.
        """
        async with self._session_factory() as session:
            row = await session.get(Outbox, job_id)
            if row is None:
                await session.commit()
                return False
            row.status = OutboxStatus.QUEUED
            row.attempts = 0
            row.locked_at = None
            row.locked_by = None
            row.next_attempt_at = now()
            await session.commit()
        logger.info("outbox.requeued", job_id=str(job_id))
        return True

    async def requeue_for_hash(self, dedupe_key: str) -> bool:
        """Requeue the job that anchored a given hash, whatever state it is in.

        This is the reorg path: the anchor was real, the block that carried it
        is not, so the same job runs again.
        """
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(Outbox).where(Outbox.dedupe_key == dedupe_key)
                )
            ).scalar_one_or_none()
            if row is None:
                await session.commit()
                return False
            row.status = OutboxStatus.QUEUED
            row.attempts = 0
            row.locked_at = None
            row.locked_by = None
            row.next_attempt_at = now()
            await session.commit()
        logger.warning("outbox.requeued_after_reorg", dedupe_key=dedupe_key)
        return True

    # -------------------------------------------------------------- helpers

    def _backoff_from(self, attempts: int) -> datetime:
        """``now() + min(2**attempts, cap)`` seconds, jittered.

        Jitter is not decoration. Without it, a batch of jobs that all failed on
        the same RPC outage retries in lockstep forever, so every retry arrives
        as a thundering herd against an endpoint that is already struggling.
        """
        cap = self._settings.outbox_backoff_cap_seconds
        # Exponent clamped before the shift: 2**attempts with a large attempts
        # value builds an enormous integer before min() ever sees it.
        base = min(2 ** min(attempts, 20), cap)
        jittered = base * random.uniform(0.8, 1.2)  # noqa: S311 - scheduling, not secrets
        return now() + timedelta(seconds=jittered)


def _render_chain(chain: list[dict[str, Any]]) -> str:
    """Readable form of the error history for the ``dead_letters`` row."""
    if not chain:
        return "(no recorded attempts)"
    return "\n".join(
        f"attempt {entry.get('attempt', '?')} at {entry.get('at', '?')}: {entry.get('error', '')}"
        for entry in chain
    )
